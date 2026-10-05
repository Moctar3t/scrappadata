#!/usr/bin/env python3
"""
gpu_agent.py — the ONLY thing that runs on the Windows GPU box (TECHNICA).

It is a dumb, stateless executor. All decisions (what to ingest, which source is
the real recording, what the metadata says) are made on the Mac by ingest_gpu.py;
this file just does the three things that need the GPU/network on that machine:
download, separate, encode. It holds NO secrets — the BunnyCDN key and the Genius
token never leave the Mac, so a compromise of the Windows box cannot leak them.

PROTOCOL
  python gpu_agent.py <command> <base64-of-json-args>
  -> prints exactly one line:  __AGENT__{json result}
  The base64 arg is why this is reliable: Windows' cmd.exe quoting mangles
  accents, ampersands and quotes in track titles, which is how the old scripts
  ended up with half-written paths. Nothing here is shell-interpolated.

Every result carries "ok": bool. On failure "error" is a real message, never a
silent default — the Mac refuses to advance a track's status without proof.
"""
from __future__ import annotations
import base64, json, os, re, shutil, subprocess, sys, time, wave
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HOME = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".")
ROOT = Path(os.environ.get("ZINGY_INGEST_ROOT") or (HOME / "zingy_ingest"))
SRC = ROOT / "src"            # decoded source audio, one .wav per track key
SEP = ROOT / "sep"            # demucs output root
ENC = ROOT / "enc"            # encoded .m4a stems, ready for the Mac to pull
# Netscape cookies.txt exported from a browser signed in on youtube.com. Used
# ONLY when a download fails on a sign-in wall (age gate / bot check).
COOKIES = Path(os.environ.get("ZINGY_COOKIES") or (ROOT / "cookies.txt"))
JAR_ROTATED = re.compile(r"cookies are no longer valid|have likely been rotated", re.I)
# A public, permanently age-gated upload used only to PROBE whether the jar still
# carries a signed-in session (no download). Override if it ever disappears.
JAR_PROBE_URL = os.environ.get("ZINGY_JAR_PROBE", "https://www.youtube.com/watch?v=AGrDHYmhuS0")


class jar_lock:
    """Cross-process lock around the cookie jar (three fetches run at once)."""
    def __enter__(self):
        import msvcrt
        self.fd = os.open(str(COOKIES) + ".lock", os.O_RDWR | os.O_CREAT)
        for _ in range(1200):                       # up to ~10 min
            try:
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1); return self
            except OSError:
                time.sleep(0.5)
        raise RuntimeError("cookie jar lock timed out")
    def __exit__(self, *a):
        import msvcrt
        try:
            os.lseek(self.fd, 0, 0); msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(self.fd)
VENV_PY = HOME / "demucs_relay" / "venv" / "Scripts" / "python.exe"
MODEL = "htdemucs_ft"
STEMS = ["bass", "drums", "other", "vocals"]

def py() -> str:
    return str(VENV_PY) if VENV_PY.exists() else sys.executable

def ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"

def ffprobe() -> str:
    return shutil.which("ffprobe") or "ffprobe"

def emit(obj):
    # ensure_ascii=True is load-bearing: this box's console is cp850 (French
    # Windows), and anything outside that codepage is destroyed on the way out
    # over SSH. A YouTube title written in Unicode math-italic
    # ("𝟏𝟏𝟐 - 𝑷𝒆𝒂𝒄𝒉𝒆𝒔 & 𝑪𝒓𝒆𝒂𝒎") arrived on the Mac as "-  &  ( )" — every letter
    # gone — which then defeats any title verification downstream. Escaping to
    # \uXXXX keeps the payload pure ASCII, so the codepage cannot touch it.
    print("__AGENT__" + json.dumps(obj, ensure_ascii=True))
    sys.stdout.flush()

def sh(cmd, timeout):
    """Run a command with a HARD timeout. The old pipeline ran demucs with no
    timeout at all, so one wedged separation hung the entire run forever."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except Exception as e:
        return 1, "", str(e)

def audio_duration(p: Path) -> float | None:
    """Real duration of a file. ffprobe first; for .wav fall back to the header so
    a missing ffprobe can never turn into a fabricated 0."""
    rc, out, _ = sh([ffprobe(), "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(p)], 60)
    if rc == 0:
        try:
            return float(out.strip())
        except ValueError:
            pass
    if p.suffix.lower() == ".wav":
        try:
            with wave.open(str(p), "rb") as w:
                return w.getnframes() / float(w.getframerate())
        except Exception:
            return None
    return None

# ── commands ────────────────────────────────────────────────────────────────
def cmd_probe(a):
    probe_src = ("import demucs,torch;"
                 "print(demucs.__version__, torch.__version__, torch.cuda.is_available())")
    rc, out, err = sh([py(), "-c", probe_src], 180)
    free = None
    try:
        free = round(shutil.disk_usage(str(HOME)).free / 1e9, 1)
    except Exception:
        pass
    rc2, ytv, _ = sh([py(), "-m", "yt_dlp", "--version"], 120)
    gpu = ""
    rc3, g, _ = sh(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], 60)
    if rc3 == 0:
        gpu = g.strip()
    emit({"ok": rc == 0, "torch_line": out.strip(), "torch_err": err.strip()[-200:],
          "ytdlp": ytv.strip() if rc2 == 0 else "", "ffmpeg": bool(shutil.which("ffmpeg")),
          "ffprobe": bool(shutil.which("ffprobe")), "free_gb": free, "gpu": gpu,
          "venv": VENV_PY.exists(), "root": str(ROOT)})

def cmd_search(a):
    """Return candidate sources WITH their durations. The Mac does the accepting —
    the agent never decides what the 'right' recording is."""
    q = f"ytsearch{int(a.get('limit', 10))}:{a['query']}"
    rc, out, err = sh([py(), "-m", "yt_dlp", "--no-warnings", "--flat-playlist",
                       "--print", "%(webpage_url)s|||%(title)s|||%(duration)s|||%(channel)s",
                       q], int(a.get("timeout", 150)))
    cands = []
    for line in out.splitlines():
        parts = line.split("|||")
        if len(parts) < 4:
            continue
        url, title, dur, ch = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
        try:
            d = float(dur)
        except ValueError:
            d = None
        if url.startswith("http"):
            cands.append({"url": url, "title": title, "duration": d, "channel": ch})
    emit({"ok": bool(cands), "candidates": cands, "error": "" if cands else (err.strip()[-300:] or "no results")})

def cmd_fetch(a):
    """Download the chosen source and decode to WAV.

    Deliberately NOT '-x --audio-format mp3' like the old pipeline: that added a
    whole extra lossy generation before demucs ever saw the audio. bestaudio is
    already lossy (YouTube opus/aac); decoding it straight to WAV keeps that the
    ONLY lossy step until the final m4a encode."""
    key, url = a["key"], a["url"]
    SRC.mkdir(parents=True, exist_ok=True)
    out = SRC / f"{key}.wav"
    if out.exists() and out.stat().st_size > 1000:
        d = audio_duration(out)
        emit({"ok": True, "cached": True, "duration": d, "size": out.stat().st_size}); return
    tmpl = str(SRC / f"{key}.%(ext)s")
    base = [py(), "-m", "yt_dlp", "--no-warnings", "--no-playlist", "--no-part",
            "-f", "bestaudio/best", "--socket-timeout", "20", "--retries", "3",
            "--extract-audio", "--audio-format", "wav",
            "--postprocessor-args", "ffmpeg:-ar 44100 -ac 2",
            "-o", tmpl]
    last = ""
    gated = False                 # any attempt hit YouTube's age gate
    clients = (None, "android", "ios", "web_safari", "tv")

    def attempt(extra, warnings=False):
        nonlocal last, gated
        # Different player clients dodge YouTube's 403 on different days; try a
        # few before calling it a real failure.
        for client in clients:
            cmd = ([c for c in base if warnings is False or c != "--no-warnings"] + extra
                   + (["--extractor-args", f"youtube:player_client={client}"] if client else [])
                   + [url])
            rc, so, se = sh(cmd, int(a.get("timeout", 420)))
            if rc == 0 and out.exists() and out.stat().st_size > 1000:
                emit({"ok": True, "cached": False, "duration": audio_duration(out),
                      "size": out.stat().st_size, "client": client or "default",
                      "cookies": bool(extra)})
                return True
            full = (se or so)
            gated = gated or bool(re.search(r"confirm your age|age-restricted", full, re.I))
            last = full.strip()[-300:]
        return False

    if attempt([]):
        return
    # Age-gated videos ("Sign in to confirm your age") and the bot wall ("Sign in
    # to confirm you're not a bot") both need a signed-in session. Only reach for
    # the jar on those errors, so an ordinary download never exposes the account.
    # ONE master jar, serialised: Google rotates the session tokens on every use
    # and yt-dlp writes the rotated ones back — a private copy per fetch would
    # throw them away and the master would be dead after its first use.
    jar_invalid = False
    if COOKIES.exists() and re.search(r"sign in|cookies", last, re.I):
        with jar_lock():
            clients = (None, "web_safari", "tv")     # android/ios ignore cookies
            if attempt(["--cookies", str(COOKIES)], warnings=True):
                return
        jar_invalid = bool(JAR_ROTATED.search(last))
    emit({"ok": False, "error": f"download failed: {last}", "jar_invalid": jar_invalid,
          "age_gated": gated})

def cmd_jar_check(a):
    """Is the jar a live signed-in session? Probes one age-gated video in
    simulate mode under the lock (so yt-dlp can write rotated tokens back)."""
    if not COOKIES.exists():
        emit({"ok": True, "present": False, "signed_in": False, "detail": "no cookies.txt on the box"}); return
    with jar_lock():
        rc, so, se = sh([py(), "-m", "yt_dlp", "-s", "--no-playlist", "--cookies", str(COOKIES),
                         "--print", "title", JAR_PROBE_URL], 90)
    msg = (se or so).strip()[-300:]
    rotated = bool(JAR_ROTATED.search(msg))
    signed = rc == 0 and not rotated and not re.search(r"confirm your age", msg, re.I)
    emit({"ok": True, "present": True, "signed_in": signed, "rotated": rotated,
          "age": int(time.time() - COOKIES.stat().st_mtime), "detail": "" if signed else msg[-160:]})

def cmd_split(a):
    key = a["key"]
    src = SRC / f"{key}.wav"
    if not src.exists():
        emit({"ok": False, "error": "source wav missing — run fetch first"}); return
    outdir = SEP / MODEL / key
    have = {s: (outdir / f"{s}.wav") for s in STEMS}
    if all(p.exists() and p.stat().st_size > 1000 for p in have.values()):
        emit({"ok": True, "cached": True}); return
    SEP.mkdir(parents=True, exist_ok=True)
    cmd = [py(), "-m", "demucs", "-n", MODEL, "-o", str(SEP), str(src)]
    if a.get("device"):
        cmd += ["-d", a["device"]]
    if a.get("segment"):
        cmd += ["--segment", str(a["segment"])]
    rc, so, se = sh(cmd, int(a.get("timeout", 1800)))
    missing = [s for s, p in have.items() if not (p.exists() and p.stat().st_size > 1000)]
    if missing:
        tail = (se or so).strip()[-400:]
        oom = "out of memory" in tail.lower() or "CUDA" in tail
        emit({"ok": False, "error": f"demucs failed (missing {missing}): {tail}", "oom": oom}); return
    emit({"ok": True, "cached": False})

def cmd_encode(a):
    """WAV stems -> AAC/m4a, ONE lossy encode, matching the catalogue's existing
    44.1 kHz stereo ~128 kbps stems."""
    key = a["key"]
    br = str(a.get("bitrate", "128k"))
    indir = SEP / MODEL / key
    outdir = ENC / key
    outdir.mkdir(parents=True, exist_ok=True)
    files, errors = {}, []
    for s in STEMS:
        src, dst = indir / f"{s}.wav", outdir / f"{s}.m4a"
        if dst.exists() and dst.stat().st_size > 1000:
            files[s] = {"size": dst.stat().st_size}
            continue
        if not src.exists():
            errors.append(f"{s}: stem wav missing"); continue
        rc, _, se = sh([ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
                        "-c:a", "aac", "-b:a", br, "-ar", "44100", "-ac", "2",
                        "-movflags", "+faststart", str(dst)], int(a.get("timeout", 300)))
        if rc != 0 or not dst.exists() or dst.stat().st_size <= 1000:
            errors.append(f"{s}: {se.strip()[-160:] or 'encode produced nothing'}")
        else:
            files[s] = {"size": dst.stat().st_size}
    ok = len(files) == len(STEMS) and not errors
    emit({"ok": ok, "files": files, "error": "; ".join(errors),
          "dir": str(outdir), "duration": audio_duration(SRC / f"{key}.wav")})

ANALYZE_SRC = r"""
import json, sys, warnings
warnings.filterwarnings("ignore")
import numpy as np, librosa
path, offset, window = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
y, sr = librosa.load(path, sr=22050, mono=True, offset=offset, duration=window)
if y.size < sr * 5:
    print(json.dumps({"ok": False, "error": "too little audio to analyse"})); raise SystemExit
tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
bpm = float(tempo[0] if hasattr(tempo, "__len__") else tempo)
h = librosa.effects.harmonic(y, margin=3.0)
chroma = librosa.feature.chroma_cens(y=h, sr=sr)
prof = np.median(chroma, axis=1)
if prof.sum() <= 0:
    print(json.dumps({"ok": False, "error": "no harmonic content"})); raise SystemExit
P = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
maj = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
mino = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
pz = (prof - prof.mean()) / (prof.std() or 1.0)
sc = []
for i in range(12):
    for pr, mode in ((maj, "Major"), (mino, "Minor")):
        pn = (pr - pr.mean()) / pr.std()
        sc.append((float(np.dot(pz, np.roll(pn, i))) / 12.0, P[i], mode))
sc.sort(reverse=True)
print(json.dumps({"ok": True, "bpm": int(round(bpm)), "key": f"{sc[0][1]} {sc[0][2]}",
                  "key_confidence": round(sc[0][0] - sc[1][0], 3)}))
"""

def cmd_analyze(a):
    """Measure tempo and key from the source audio.

    Runs in the DEMUCS VENV via a subprocess, not in this process: the agent is
    launched with the box's system python, which has no numpy or librosa, so an
    inline import fails with "No module named 'numpy'" no matter what the venv
    contains.

    Needed because Spotify closed audio-features to new apps in Nov 2024, so a
    song added by album/track lookup carries no tempo or key, and the app shows
    both on every song. Analysing here is free — the decoded WAV is already on
    this box from the fetch step, so no audio has to move.
    """
    key = a["key"]
    src = SRC / f"{key}.wav"
    if not src.exists():
        emit({"ok": False, "error": "source wav missing — analyze must follow fetch"}); return
    script = ROOT / "_analyze.py"
    try:
        script.write_text(ANALYZE_SRC, encoding="utf-8")
    except Exception as e:
        emit({"ok": False, "error": f"could not write helper: {e}"}); return
    rc, out, err = sh([py(), str(script), str(src),
                       str(a.get("offset", 0)), str(a.get("window", 150))],
                      int(a.get("timeout", 420)))
    line = next((l for l in out.splitlines() if l.strip().startswith("{")), "")
    if rc != 0 or not line:
        emit({"ok": False, "error": f"analyze failed: {(err or out).strip()[-200:]}"}); return
    try:
        res = json.loads(line)
    except json.JSONDecodeError as e:
        emit({"ok": False, "error": f"unparseable analyze output: {e}"}); return
    res["duration"] = audio_duration(src)
    emit(res)

def cmd_cleanup(a):
    """Free disk for the intermediates of tracks the Mac has already pulled."""
    removed = []
    for key in a.get("keys", []):
        for p in (SRC / f"{key}.wav", SEP / MODEL / key, ENC / key):
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True); removed.append(str(p))
                elif p.exists():
                    p.unlink(); removed.append(str(p))
            except Exception:
                pass
    free = None
    try:
        free = round(shutil.disk_usage(str(HOME)).free / 1e9, 1)
    except Exception:
        pass
    emit({"ok": True, "removed": len(removed), "free_gb": free})

COMMANDS = {"probe": cmd_probe, "search": cmd_search, "fetch": cmd_fetch,
            "split": cmd_split, "encode": cmd_encode, "analyze": cmd_analyze,
            "cleanup": cmd_cleanup,
    "jar_check": cmd_jar_check,
}

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        emit({"ok": False, "error": f"usage: gpu_agent.py {'|'.join(COMMANDS)} <b64json>"}); return
    args = {}
    if len(sys.argv) > 2:
        try:
            args = json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))
        except Exception as e:
            emit({"ok": False, "error": f"bad args: {e}"}); return
    try:
        COMMANDS[sys.argv[1]](args)
    except Exception as e:
        emit({"ok": False, "error": f"{type(e).__name__}: {e}"})

if __name__ == "__main__":
    main()
