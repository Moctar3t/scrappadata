#!/usr/bin/env python3
"""
vps_line.py - the Replicate line's worker, on the VPS. No home GPU anywhere.

What the GPU box's gpu_agent.py does for the box line (search, fetch, split,
encode, measure), done here with the split on Replicate, the same model the
server already uses for listeners' uploads (ryan5453/demucs, htdemucs_ft):

  search   YouTube candidates with their durations (the Mac does the accepting)
  produce  fetch the chosen upload, check its real runtime, split it on
           Replicate (FLAC out, lossless), encode each stem once to AAC 128k
           (the catalogue's format), measure BPM and key from the stems
  cleanup  forget a track's files

YouTube refuses this machine's address ("Sign in to confirm you're not a bot"),
so every YouTube request goes through `proxy`: a SOCKS tunnel the Mac opens
out of its own connection for the length of a stage (replicate_line.Tunnel).

The Replicate token is read from the API's .env here and never leaves this
machine. Protocol, like gpu_agent.py: `vps_line.py <command> <base64 json>`,
one line out: __LINE__{json}, always with "ok".
"""
from __future__ import annotations
import base64, json, os, re, shutil, subprocess, sys, time, urllib.error, urllib.request, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ZINGY_LINE_ROOT", "/var/tmp/zingy_line"))
ENV_FILE = Path("/var/www/zingy-api/.env")
YTDLP = shutil.which("yt-dlp") or "/usr/local/bin/yt-dlp"
STEMS = ["bass", "drums", "other", "vocals"]
CLIENTS = (None, "visionos", "android_vr", "web_safari", "tv", "ios")
REPLICATE = "https://api.replicate.com/v1"

def emit(obj) -> None:
    print("__LINE__" + json.dumps(obj, ensure_ascii=True), flush=True)

def sh(cmd, timeout):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"

def env(name: str) -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError(f"{name} missing in {ENV_FILE}")

def duration(p: Path) -> float | None:
    rc, out, _ = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(p)], 60)
    try:
        return float(out.strip()) if rc == 0 else None
    except ValueError:
        return None

def ytdlp(a: dict) -> list[str]:
    return [YTDLP, "--no-warnings", "--no-update", "--js-runtimes", "node",
            "--proxy", f"socks5h://127.0.0.1:{int(a['proxy'])}", "--socket-timeout", "20"]

# ── search ──────────────────────────────────────────────────────────────────
def cmd_search(a):
    q = f"ytsearch{int(a.get('limit', 12))}:{a['query']}"
    rc, out, err = sh(ytdlp(a) + ["--flat-playlist", "--print",
                                  "%(webpage_url)s|||%(title)s|||%(duration)s|||%(channel)s", q],
                      int(a.get("timeout", 150)))
    cands = []
    for line in out.splitlines():
        parts = line.split("|||")
        if len(parts) < 4 or not parts[0].startswith("http"):
            continue
        try:
            d = float(parts[2])
        except ValueError:
            d = None
        cands.append({"url": parts[0].strip(), "title": parts[1].strip(), "duration": d,
                      "channel": parts[3].strip()})
    emit({"ok": bool(cands), "candidates": cands, "error": "" if cands else (err.strip()[-300:] or "no results")})

# ── produce ─────────────────────────────────────────────────────────────────
def fetch(a: dict, url: str, work: Path) -> tuple[Path | None, str, bool]:
    """bestaudio as YouTube serves it (opus/aac), no conversion: the one lossy
    step before the final AAC encode. Several player clients, as YouTube 403s
    a different one on different days. Returns (file, error, age_gated)."""
    last, gated = "", False
    for client in CLIENTS:
        for f in work.glob("src.*"):
            f.unlink()
        extra = ["--extractor-args", f"youtube:player_client={client}"] if client else []
        rc, so, se = sh(ytdlp(a) + ["--no-playlist", "--no-part", "-f", "bestaudio/best", "--retries", "3",
                                    "-o", str(work / "src.%(ext)s")] + extra + [url], 420)
        got = [f for f in work.glob("src.*") if f.stat().st_size > 10_000]
        if rc == 0 and got:
            return got[0], "", False
        full = se or so
        gated = gated or bool(re.search(r"confirm your age|age-restricted", full, re.I))
        last = full.strip()[-300:]
    return None, last, gated

def replicate_split(src: Path, work: Path, token: str, version: str) -> dict:
    """Upload, predict, download the four FLAC stems. Raises on failure."""
    hdr = {"Authorization": f"Bearer {token}"}
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"content\"; filename=\"{src.name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + src.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    up = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{REPLICATE}/files", data=body, method="POST",
        headers={**hdr, "Content-Type": f"multipart/form-data; boundary={boundary}"}), timeout=180).read())
    file_id, file_url = up["id"], up["urls"]["get"]
    try:
        req = json.dumps({"version": version, "input": {
            "audio": file_url, "model": "htdemucs_ft", "output_format": "flac", "jobs": 0}}).encode()
        pred = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"{REPLICATE}/predictions", data=req, method="POST",
            headers={**hdr, "Content-Type": "application/json"}), timeout=60).read())
        started, fails = time.time(), 0
        while True:
            time.sleep(4)
            try:
                pred = json.loads(urllib.request.urlopen(urllib.request.Request(
                    f"{REPLICATE}/predictions/{pred['id']}", headers=hdr), timeout=60).read())
                fails = 0
            except (urllib.error.URLError, TimeoutError) as e:
                fails += 1
                if fails >= 10:
                    raise RuntimeError(f"replicate poll kept failing: {e}")
                continue
            if pred["status"] == "succeeded":
                break
            if pred["status"] in ("failed", "canceled"):
                raise RuntimeError(f"replicate {pred['status']}: {str(pred.get('error'))[:200]}")
            if time.time() - started > 1800:
                raise RuntimeError("replicate took over 30 min")
        out = pred.get("output") or {}
        for s in STEMS:
            if not out.get(s):
                raise RuntimeError(f"replicate output has no {s}")
            with urllib.request.urlopen(out[s], timeout=180) as r, open(work / f"{s}.flac", "wb") as f:
                shutil.copyfileobj(r, f)
        return {"prediction": pred["id"],
                "predict_time": (pred.get("metrics") or {}).get("predict_time")}
    finally:
        try:
            urllib.request.urlopen(urllib.request.Request(f"{REPLICATE}/files/{file_id}",
                                                          method="DELETE", headers=hdr), timeout=30)
        except Exception:
            pass

def cmd_produce(a):
    os.nice(10)   # the live stream's encoder on this machine comes first
    key = str(a["key"])
    work = ROOT / key
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    want = float(a.get("want_s") or 0)
    tol = float(a.get("tolerance", 5))

    # The chosen upload, then the runners-up that passed the same gate (an age
    # gate is a property of the upload: the label's "- Topic" copy is often open).
    src, err, used, gated_any = None, "", None, False
    for url in a["urls"]:
        src, err, gated = fetch(a, url, work)
        gated_any = gated_any or gated
        if src:
            used = url; break
        if not gated:
            break
    if not src:
        emit({"ok": False, "error": f"download failed: {err}", "age_gated": gated_any}); return
    got = duration(src)
    if got and want and abs(got - want) > tol:
        shutil.rmtree(work, ignore_errors=True)
        emit({"ok": False, "wrong_length": True, "duration": got,
              "error": f"downloaded audio is {got:.0f}s but Spotify says {want:.0f}s, wrong recording, discarded"})
        return
    try:
        rep = replicate_split(src, work, env("REPLICATE_API_TOKEN"), env("REPLICATE_MODEL_VERSION"))
    except urllib.error.HTTPError as e:
        emit({"ok": False, "error": f"replicate {e.code}: {e.read().decode('utf-8', 'replace')[:200]}",
              "unavailable": e.code in (429,) or e.code >= 500}); return
    except Exception as e:
        emit({"ok": False, "error": str(e)[:300], "unavailable": isinstance(e, (urllib.error.URLError, TimeoutError))})
        return
    files = {}
    for s in STEMS:
        out = work / f"{s}.m4a"
        rc, _, se = sh(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(work / f"{s}.flac"),
                        "-c:a", "aac", "-b:a", str(a.get("bitrate", "128k")), "-ar", "44100", "-ac", "2",
                        "-movflags", "+faststart", str(out)], 300)
        if rc != 0 or not out.exists():
            emit({"ok": False, "error": f"encode {s}: {se.strip()[-200:]}"}); return
        files[s] = {"path": str(out), "size": out.stat().st_size}
    res = {"ok": True, "duration": got, "url": used, "via_alternate": used != a["urls"][0],
           "files": files, **rep}
    if a.get("analyze"):
        sys.path.insert(0, str(HERE))
        import vps_analyze
        m, aerr = vps_analyze.analyse_files({s: work / f"{s}.m4a" for s in STEMS})
        if m:
            res["analysis"] = m
        else:
            res["analysis_error"] = aerr
    for s in STEMS:
        (work / f"{s}.flac").unlink(missing_ok=True)
    src.unlink(missing_ok=True)
    emit(res)

# ── info: what a link IS, for link_intake.py ────────────────────────────────
INFO_KEYS = ("id", "title", "channel", "uploader", "duration", "track", "artist", "artists",
             "album", "release_date", "release_year", "thumbnail", "age_limit")

def cmd_info(a):
    """One video's metadata (YouTube Music's track/artist/album when it has them),
    or a playlist's video ids. Read here because this yt-dlp is current; the Mac's
    Python is too old for one that still reads YouTube."""
    url = a["url"]
    if a.get("playlist"):
        rc, out, err = sh(ytdlp(a) + ["--flat-playlist", "--print", "%(id)s", url],
                          int(a.get("timeout", 150)))
        ids = [l.strip() for l in out.splitlines() if len(l.strip()) == 11]
        emit({"ok": bool(ids), "ids": ids, "error": "" if ids else (err.strip()[-300:] or "empty playlist")})
        return
    rc, out, err = sh(ytdlp(a) + ["--no-playlist", "--skip-download", "--dump-json", url],
                      int(a.get("timeout", 120)))
    if rc != 0 or not out.strip():
        emit({"ok": False, "error": (err.strip() or "no metadata")[-400:]}); return
    info = json.loads(out.splitlines()[-1])
    emit({"ok": True, "info": {k: info.get(k) for k in INFO_KEYS if info.get(k) is not None}})

def cmd_cleanup(a):
    for k in a.get("keys", []):
        shutil.rmtree(ROOT / str(k), ignore_errors=True)
    emit({"ok": True})

if __name__ == "__main__":
    cmd, blob = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""
    args = json.loads(base64.b64decode(blob).decode("utf-8")) if blob else {}
    try:
        {"search": cmd_search, "produce": cmd_produce, "info": cmd_info, "cleanup": cmd_cleanup}[cmd](args)
    except Exception as e:
        emit({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
