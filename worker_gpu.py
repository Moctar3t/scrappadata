#!/usr/bin/env python3
"""
Zingy home-GPU stem worker (BATCH model) — hardened.

Runs on the Windows RTX box. Polls the VPS for queued tracks, downloads them in
parallel, computes BPM/key, feeds the whole batch through one Demucs GPU call,
encodes + uploads the 4 stems to BunnyCDN, reports back.

WHAT WAS BROKEN BEFORE (all fixed here, each with the evidence that found it):

  1. COOKIES. worker.env sets YT_COOKIES_BROWSER=chrome, so every download ran
     `--cookies-from-browser chrome` and died with
         "ERROR: Failed to decrypt with DPAPI"
     Chrome >=127 wraps its cookie DB in App-Bound Encryption, so no other
     process can read it (yt-dlp #10927). The cookies.txt sitting right next to
     worker.py was never used, because the browser branch won. Now: cookies.txt
     (resolved against THIS file, not the cwd) is the primary, the browser is an
     explicit opt-in fallback, and a broken browser extraction demotes itself
     instead of failing the track.

  2. NO RETRIES. download() was a single subprocess.run(check=True). One 403,
     one throttle, one DNS blip = the track is dead forever. Now: a ladder of
     strategies (cookies -> alternate player clients -> no cookies) with
     exponential backoff, and stderr is captured so the log says WHY.

  3. SILENT EMPTY COMPLETIONS. The API's complete() does
         { bassUrl: stems.bass, ..., status: 'ready' }
     and Prisma treats `undefined` as "leave column alone" — so posting {} marks
     a track READY with four NULL stem URLs. 16 catalogue rows are in exactly
     that state and the app shipped them as playable songs. Now: report_complete
     REFUSES to fire unless all four URLs are present and look like real URLs.

  4. DNS. "[warn] poll error: Failed to resolve 'zingy.dime.systems'" appears
     throughout worker.log. Now: a Session with connection-level retries, and a
     cached last-known-good IP used as a fallback host when resolution fails.

  5. UNBOUNDED LOG. worker.log had grown to 7.9 MB. Now rotated at 8 MB.

  6. NO LIVENESS SIGNAL. "is the worker alive?" could only be answered by
     eyeballing the log. Now: heartbeat.json is rewritten every poll, so
     auto_ingest can tell a running worker from a wedged one.
"""
from __future__ import annotations
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import librosa


def load_env(path: str) -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


HERE = Path(__file__).resolve().parent
load_env(str(HERE / "worker.env"))

API_BASE      = os.environ["ZINGY_API_BASE"].rstrip("/")
WORKER_SECRET = os.environ["ZINGY_WORKER_SECRET"]
BUNNY_HOST    = os.environ.get("BUNNY_STORAGE_HOST", "storage.bunnycdn.com")
BUNNY_ZONE    = os.environ["BUNNY_STORAGE_ZONE"]
BUNNY_KEY     = os.environ["BUNNY_STORAGE_PASSWORD"]
BUNNY_PULL    = os.environ["BUNNY_PULL_BASE"].rstrip("/")
DEMUCS_PY     = os.environ.get("DEMUCS_PYTHON", sys.executable)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SEC", "5"))
WORK_DIR      = Path(os.environ.get("WORK_DIR", str(HERE / "work")))
BATCH_MAX     = int(os.environ.get("BATCH_MAX", "20"))

STEMS = ("bass", "drums", "vocals", "other")
HDRS = {"x-worker-secret": WORKER_SECRET}

HEARTBEAT = HERE / "heartbeat.json"
DNS_CACHE = HERE / ".dns_cache.json"
LOG_PATH  = HERE / "worker.log"
LOG_MAX   = 8 * 1024 * 1024


# The Windows console is cp1252, which cannot encode most of what this worker
# prints — not just box-drawing marks but the catalogue itself: "Bande organisée",
# "1PLIKÉ140", Japanese city-pop titles. Any of them raises UnicodeEncodeError
# inside print() and kills the worker mid-batch. Force UTF-8 on the streams.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── network: retries + a DNS fallback ────────────────────────────────────────
# The box loses DNS for zingy.dime.systems intermittently. Requests' own retry
# does not cover NameResolutionError, so we remember the last IP that worked and
# retry against it with an explicit Host header when resolution fails.
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=5, connect=5, read=3, backoff_factor=1.5,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=frozenset(["GET", "POST", "PUT"]))
    ad = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    s.mount("https://", ad)
    s.mount("http://", ad)
    return s


SESSION = make_session()


def _api_host() -> str:
    return API_BASE.split("://", 1)[-1].split("/", 1)[0]


def _remember_ip() -> None:
    try:
        ip = socket.gethostbyname(_api_host())
        DNS_CACHE.write_text(json.dumps({"host": _api_host(), "ip": ip,
                                         "at": time.time()}))
    except Exception:
        pass


def _cached_ip() -> str | None:
    try:
        d = json.loads(DNS_CACHE.read_text())
        return d.get("ip") if d.get("host") == _api_host() else None
    except Exception:
        return None


def api(method: str, path: str, **kw):
    """One API call, with a literal-IP retry when DNS is the thing that broke."""
    url = f"{API_BASE}{path}"
    kw.setdefault("headers", {}).update(HDRS)
    kw.setdefault("timeout", 30)
    try:
        r = SESSION.request(method, url, **kw)
        r.raise_for_status()
        _remember_ip()
        return r
    except requests.exceptions.ConnectionError as e:
        ip = _cached_ip()
        if not ip or "NameResolution" not in str(e) and "getaddrinfo" not in str(e):
            raise
        host = _api_host()
        log(f"[dns] {host} did not resolve — retrying via last-known-good {ip}")
        kw["headers"]["Host"] = host
        kw["verify"] = False
        r = SESSION.request(method, url.replace(f"https://{host}", f"https://{ip}"), **kw)
        r.raise_for_status()
        return r


def claim_next():
    try:
        data = api("GET", "/internal/split/next").json()
    except Exception as e:
        log(f"[warn] claim failed: {e}")
        return None
    return data if data.get("id") else None


def report_complete(track_id: str, urls: dict) -> None:
    """The API marks a track ready and writes whatever URLs it is handed. Prisma
    reads `undefined` as 'do not touch this column', so an empty payload produces
    a READY track with four NULL stem URLs — a song that shows up in the app and
    plays nothing. 16 rows in the live catalogue are in exactly that state.
    Refuse to be the source of another one."""
    missing = [s for s in STEMS if not str(urls.get(s, "")).startswith("http")]
    if missing:
        raise RuntimeError(f"refusing to report ready — missing stem urls: {missing}")
    api("POST", f"/internal/split/{track_id}/complete", json=urls, timeout=60)


def report_fail(track_id: str, error: str) -> None:
    try:
        api("POST", f"/internal/split/{track_id}/fail",
            json={"error": error[:500]}, timeout=30)
    except Exception:
        pass


def is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


# ── cookies ──────────────────────────────────────────────────────────────────
COOKIES = HERE / "cookies.txt"          # absolute — NOT relative to the cwd
COOKIES_OK = False                      # set by preflight()
COOKIES_WHY = "not checked yet"


# YouTube proves sign-in with cookies scoped to .youtube.com — LOGIN_INFO above
# all. A jar exported from a google.com context carries SID/HSID/SAPISID on
# .google.com and looks convincingly complete while authenticating NOTHING: every
# age-gated video then fails with "Sign in to confirm your age" no matter which
# player client is tried. That is exactly the state the box was in, and the first
# version of this check called it "ok" because it only counted cookie names.
YT_AUTH = ("LOGIN_INFO", "SID", "HSID", "SSID", "APISID", "SAPISID",
           "__Secure-1PSID", "__Secure-3PSID")


def cookie_report() -> tuple[bool, str]:
    """(usable_for_age_gated, human readable). Checked at startup so a logged-out
    jar is announced once, not rediscovered as six mystery failures."""
    if not COOKIES.exists():
        return False, "MISSING — age-restricted videos will fail"
    try:
        txt = COOKIES.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return False, f"unreadable ({e})"

    yt_scoped: set[str] = set()
    total = 0
    for line in txt.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        total += 1
        if "youtube.com" in parts[0] and parts[5] in YT_AUTH:
            yt_scoped.add(parts[5])

    age_d = (time.time() - COOKIES.stat().st_mtime) / 86400
    if "LOGIN_INFO" not in yt_scoped:
        return False, (f"{total} cookies but LOGGED OUT for youtube.com "
                       f"(no LOGIN_INFO; have {sorted(yt_scoped) or 'nothing'} on "
                       f".youtube.com) — age-gated videos WILL fail. Re-export "
                       f"cookies.txt from a tab signed in on youtube.com.")
    state = f"signed in ({len(yt_scoped)}/{len(YT_AUTH)} auth cookies)"
    if age_d > 45:
        state += f", {age_d:.0f} days old — re-export soon"
    return True, f"{total} cookies, {state}"


# Ordered download strategies. Each is (label, extra yt-dlp args). We stop at the
# first that produces a real file. The clients differ in which throttling and
# signature paths YouTube applies, so a failure on one is routinely a success on
# the next — that is the whole reason a single-shot download was so brittle.
def _strategies() -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    if COOKIES.exists():
        c = ["--cookies", str(COOKIES)]
        out.append(("cookies.txt", c))
        out.append(("cookies.txt + tv client",
                    c + ["--extractor-args", "youtube:player_client=tv,web_safari"]))
        # The embedded players are the ones that historically clear an age gate
        # WHEN the jar is genuinely signed in. Useless against a logged-out jar,
        # which is why download() stops early in that case instead of trying all.
        out.append(("cookies.txt + tv_embedded",
                    c + ["--extractor-args", "youtube:player_client=tv_embedded"]))
        out.append(("cookies.txt + mweb",
                    c + ["--extractor-args", "youtube:player_client=mweb,web_embedded"]))
    browser = os.environ.get("YT_COOKIES_BROWSER")
    if browser:
        # Kept last on purpose: on Windows with Chrome >=127 this raises
        # "Failed to decrypt with DPAPI" and can never work, but on a box where
        # the browser is Firefox it is still the freshest source.
        out.append((f"browser:{browser}", ["--cookies-from-browser", browser]))
    out.append(("no cookies + android", ["--extractor-args", "youtube:player_client=android"]))
    out.append(("no cookies + ios", ["--extractor-args", "youtube:player_client=ios"]))
    out.append(("plain", []))
    return out


def _is_age_gate(err: str) -> bool:
    return "confirm your age" in err.lower() or "age-restricted" in err.lower()


MIN_AUDIO_BYTES = 100_000       # anything smaller is an error page, not a song


def _downloaded_file(dest: Path) -> Path | None:
    """yt-dlp may land on dest, dest.mp3, or dest with a doubled extension."""
    for cand in (dest, dest.with_suffix(".mp3"),
                 dest.with_name(dest.name + ".mp3")):
        if cand.exists() and cand.stat().st_size >= MIN_AUDIO_BYTES:
            return cand
    parent = dest.parent
    if parent.exists():
        for f in parent.glob(f"{dest.stem}*.mp3"):
            if f.stat().st_size >= MIN_AUDIO_BYTES:
                return f
    return None


def download(url: str, dest: Path) -> None:
    if not is_youtube(url):
        with SESSION.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        if dest.stat().st_size < MIN_AUDIO_BYTES:
            raise RuntimeError(f"downloaded file is only {dest.stat().st_size} bytes")
        return

    base = [DEMUCS_PY, "-m", "yt_dlp", "-f", "bestaudio/best",
            "-x", "--audio-format", "mp3", "--audio-quality", "192K",
            "--no-playlist", "--no-progress",
            "--retries", "5", "--fragment-retries", "10",
            "--socket-timeout", "30",
            "-o", str(dest), "--remote-components", "ejs:github"]

    errors = []
    for attempt, (label, extra) in enumerate(_strategies()):
        for stale in dest.parent.glob(f"{dest.stem}*"):
            stale.unlink(missing_ok=True)
        r = subprocess.run(base + extra + [url], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
        got = _downloaded_file(dest)
        if got:
            if got != dest:
                got.replace(dest)
            if label != "cookies.txt":
                log(f"  [dl] succeeded via '{label}' (attempt {attempt + 1})")
            return
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        why = tail[-1][:180] if tail else f"exit {r.returncode}, no output file"
        errors.append(f"{label}: {why}")
        log(f"  [dl] '{label}' failed — {why}")

        # An age gate is a credential problem, not a transport problem. With a
        # logged-out jar NO player client can clear it, so grinding through six
        # more attempts just burns two minutes to reach the same answer. Say the
        # real reason once and stop.
        if _is_age_gate(why) and not COOKIES_OK:
            raise RuntimeError(
                "age-restricted video and cookies.txt is not signed in to YouTube "
                f"({COOKIES_WHY}). Re-export cookies.txt from a browser tab logged "
                "in on youtube.com, then requeue.")
        time.sleep(min(2 ** attempt, 20))

    raise RuntimeError("all download strategies failed :: " + " | ".join(errors))


def _tempo_octave_fix(y, sr, bpm: float) -> int:
    """Resolve the tempo-octave ambiguity with evidence, not a rule of thumb.

    librosa reports whichever pulse the beat tracker locks onto, and for trap and
    drill that is routinely the half-time pulse — 285 songs in the catalogue sit
    at 70-77 BPM when they are really 140-154. But blindly doubling anything
    under 78 would wreck a genuine 68 BPM ballad, so instead: score the onset
    envelope's autocorrelation at the candidate tempo and at its double/half, and
    keep whichever the audio actually supports."""
    if not bpm or bpm <= 0:
        return int(bpm or 0)
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    hop = 512
    ac = librosa.autocorrelate(onset, max_size=len(onset))
    if ac.size == 0 or ac[0] <= 0:
        return int(round(bpm))
    ac = ac / ac[0]

    def strength(cand: float) -> float:
        if cand <= 0:
            return -1.0
        lag = (60.0 / cand) * sr / hop         # frames per beat at this tempo
        i = int(round(lag))
        if i < 1 or i >= len(ac):
            return -1.0
        return float(np.max(ac[max(1, i - 1): i + 2]))

    best, best_s = bpm, strength(bpm)
    for cand in (bpm * 2, bpm / 2):
        if 55 <= cand <= 200 and strength(cand) > best_s * 1.02:
            best, best_s = cand, strength(cand)
    return int(round(best))


def extract_bpm_key(file_path: Path) -> tuple:
    warnings.filterwarnings("ignore", category=UserWarning)
    y, sr = librosa.load(str(file_path), sr=22050)

    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    raw = float(tempo[0]) if isinstance(tempo, np.ndarray) else float(tempo)
    bpm = _tempo_octave_fix(y, sr, raw)
    if bpm != round(raw):
        log(f"  [bpm] {raw:.0f} -> {bpm} (tempo-octave corrected)")

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_vals = np.sum(chroma, axis=1)

    pitches = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    maj = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    mino = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    maj = maj / np.linalg.norm(maj)
    mino = mino / np.linalg.norm(mino)
    chroma_vals = chroma_vals / np.linalg.norm(chroma_vals)

    best_corr, best_key, best_mode = -1, "C", "Major"
    for i in range(12):
        mc = np.corrcoef(chroma_vals, np.roll(maj, i))[0, 1]
        nc = np.corrcoef(chroma_vals, np.roll(mino, i))[0, 1]
        if mc > best_corr:
            best_corr, best_key, best_mode = mc, pitches[i], "Major"
        if nc > best_corr:
            best_corr, best_key, best_mode = nc, pitches[i], "Minor"
    return bpm, f"{best_key} {best_mode}"


def bunny_put(remote_path: str, local_file: Path, content_type="audio/mpeg") -> str:
    url = f"https://{BUNNY_HOST}/{BUNNY_ZONE}/{remote_path}"
    last = None
    for attempt in range(4):
        try:
            with open(local_file, "rb") as f:
                r = SESSION.put(url, data=f, timeout=300,
                                headers={"AccessKey": BUNNY_KEY, "Content-Type": content_type})
            if r.ok:
                return f"{BUNNY_PULL}/{remote_path}"
            last = f"{r.status_code}: {r.text[:150]}"
        except Exception as e:
            last = str(e)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"bunny put failed after 4 tries — {last}")


def encode(wav: Path, mp3: Path, m4a: Path) -> None:
    for args, out in ((["-c:a", "libmp3lame", "-b:a", "192k"], mp3),
                      (["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"], m4a)):
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                        "-i", str(wav)] + args + [str(out)], check=True)
        if not out.exists() or out.stat().st_size < 10_000:
            raise RuntimeError(f"encode produced an empty file: {out.name}")


upload_executor = ThreadPoolExecutor(max_workers=8)


def process_batch(jobs: list):
    job_details = {}
    for j in jobs:
        tid = j["id"]
        job_dir = WORK_DIR / tid
        job_dir.mkdir(parents=True, exist_ok=True)
        job_details[tid] = {"job": j, "dir": job_dir,
                            "in_path": job_dir / f"{tid}.mp3",
                            "bpm": j.get("bpm"), "key": j.get("songKey")}

    log(f"--- [BATCH DOWNLOAD & ANALYZE] {len(jobs)} tracks ---")

    def dl_and_analyze(tid):
        d = job_details[tid]
        try:
            download(d["job"]["originalUrl"], d["in_path"])
            if is_youtube(d["job"]["originalUrl"]) and (not d["bpm"] or not d["key"]):
                bpm, key = extract_bpm_key(d["in_path"])
                d["bpm"], d["key"] = d["bpm"] or bpm, d["key"] or key
            return tid, None
        except Exception as e:
            return tid, str(e)

    successful = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(dl_and_analyze, t): t for t in job_details}
        for fut in as_completed(futs):
            tid, err = fut.result()
            if err:
                log(f"[fail]  {tid} during download/analyze: {err[:300]}")
                report_fail(tid, err)
            else:
                successful.append(tid)
                log(f"[ready] {tid} downloaded & analyzed")

    if not successful:
        return

    log(f"--- [BATCH DEMUCS GPU] {len(successful)} tracks ---")
    out_dir = WORK_DIR / "batch_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [DEMUCS_PY, "-m", "demucs.separate", "-n", "htdemucs_ft",
           "--device", "cuda", "-o", str(out_dir)] + \
          [str(job_details[t]["in_path"]) for t in successful]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=3600)
    except Exception as e:
        # One bad file should not kill the batch. Retry each track alone so the
        # rest of the batch still lands.
        log(f"[warn] batch demucs failed ({e}) — retrying tracks individually")
        survivors = []
        for tid in successful:
            solo = [DEMUCS_PY, "-m", "demucs.separate", "-n", "htdemucs_ft",
                    "--device", "cuda", "-o", str(out_dir),
                    str(job_details[tid]["in_path"])]
            try:
                subprocess.run(solo, check=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=1800)
                survivors.append(tid)
            except Exception as e2:
                log(f"[fail]  {tid} demucs: {e2}")
                report_fail(tid, f"demucs: {e2}")
        successful = survivors
        if not successful:
            return

    log("--- [OFFLOADING ENCODE & UPLOAD TO BACKGROUND] ---")

    def enc_and_upload(tid, d, stem_root):
        try:
            audio_url = d["job"]["originalUrl"]
            base = f"{'catalogtracks' if is_youtube(audio_url) else 'usertracks'}/{tid}"
            urls = {}
            for s in STEMS:
                wav = stem_root / f"{s}.wav"
                if not wav.exists():
                    raise RuntimeError(f"missing stem output: {wav}")
                mp3, m4a = d["dir"] / f"{s}.mp3", d["dir"] / f"{s}.m4a"
                encode(wav, mp3, m4a)
                urls[s] = bunny_put(f"{base}/{s}.mp3", mp3, "audio/mpeg")
                bunny_put(f"{base}/{s}.m4a", m4a, "audio/mp4")
            if d["bpm"]:
                urls["bpm"] = d["bpm"]
            if d["key"]:
                urls["songKey"] = d["key"]
            report_complete(tid, urls)          # guards all-4-present itself
            log(f"[done]  {tid} fully processed and uploaded")
        except Exception as e:
            log(f"[fail]  {tid} during upload: {e}")
            report_fail(tid, str(e))
        finally:
            shutil.rmtree(d["dir"], ignore_errors=True)
            shutil.rmtree(stem_root, ignore_errors=True)

    for tid in successful:
        d = job_details[tid]
        upload_executor.submit(enc_and_upload, tid, d,
                               out_dir / "htdemucs_ft" / d["in_path"].stem)


def beat(state: str, extra: dict | None = None) -> None:
    """Liveness file so 'is the worker alive' is answerable without reading logs."""
    try:
        HEARTBEAT.write_text(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "epoch": int(time.time()), "pid": os.getpid(), "state": state,
            **(extra or {})}))
    except Exception:
        pass


def rotate_log() -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX:
            LOG_PATH.with_suffix(".log.1").unlink(missing_ok=True)
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
    except Exception:
        pass


def preflight() -> None:
    global COOKIES_OK, COOKIES_WHY
    COOKIES_OK, COOKIES_WHY = cookie_report()
    log("=" * 62)
    log(f"[zingy-gpu-worker] api      : {API_BASE}")
    log(f"[zingy-gpu-worker] cookies  : {'OK ' if COOKIES_OK else 'BAD'} {COOKIES_WHY}")
    try:
        import torch
        log(f"[zingy-gpu-worker] cuda     : {torch.cuda.is_available()} "
            f"({torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU ONLY — will be slow'})")
    except Exception as e:
        log(f"[zingy-gpu-worker] cuda     : unknown ({e})")
    if shutil.which("ffmpeg") is None:
        log("[zingy-gpu-worker] ffmpeg   : MISSING — encoding will fail")
    try:
        socket.gethostbyname(_api_host())
        log(f"[zingy-gpu-worker] dns      : {_api_host()} resolves ok")
        _remember_ip()
    except Exception:
        log(f"[zingy-gpu-worker] dns      : {_api_host()} DOES NOT RESOLVE "
            f"(cached {_cached_ip()})")
    log("=" * 62)


def main() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    preflight()
    beat("starting")
    idle = 0
    while True:
        try:
            rotate_log()
            jobs = []
            for _ in range(BATCH_MAX):
                job = claim_next()
                if not job:
                    break
                jobs.append(job)

            if not jobs:
                idle += 1
                beat("idle", {"idle_polls": idle})
                time.sleep(POLL_INTERVAL)
                continue

            idle = 0
            log(f"[claim] grabbed a batch of {len(jobs)} track(s)")
            beat("working", {"batch": len(jobs),
                             "ids": [j["id"][:8] for j in jobs]})
            process_batch(jobs)
            beat("idle")

        except KeyboardInterrupt:
            beat("stopped")
            log("bye")
            return
        except Exception as e:
            log(f"[warn] loop error: {e}\n{traceback.format_exc()[:600]}")
            beat("error", {"error": str(e)[:200]})
            time.sleep(POLL_INTERVAL * 2)


if __name__ == "__main__":
    main()
