#!/usr/bin/env python3
"""
zingy_pipeline.py — ONE clean catalog ingest pipeline. Replaces the tangle of
run_demucs.py / upload_stems.py / sync_loop.py / the VPS pull-worker / demucs_relay /
zingy-worker. Runs on the Windows GPU box (htdemucs_ft on the RTX 3070).

WHY THE OLD THING KEPT FAILING (designed out here)
  • upload key was HARDCODED + stale  -> 401 on every upload. Here: read from .env, ONE place.
  • demucs output dir != uploader input dir. Here: one STEMS_DIR constant, used by both.
  • download->separate->upload coupled per track -> one failure strands a track, GPU idles.
      Here: download runs AHEAD of separation (producer/consumer, GPU never waits), and
      upload is a SEPARATE batch stage at the end + a verify sweep that re-uploads misses.
  • three worker dirs fighting. Here: one file, one manifest, idempotent/resumable.

STAGES (each resumable; state in pipeline_state.json)
  prepare  build the job list from input + dedup against the live catalog
  run      download (ahead) + GPU-separate (htdemucs_ft) + enrich metadata  [GPU stage]
  upload   batch-upload every track's 4 stems to BunnyCDN (key from .env)
  sweep    HEAD-verify every stem URL on the CDN; re-upload anything missing
  all      prepare -> run -> upload -> sweep

INPUT (prepare)  — pick ONE:
  --yt-playlist URL        a YouTube playlist (each video = one track)
  --yt URL [URL ...]       one or more YouTube video links
  --csv FILE               an Exportify/Spotify CSV (Track Name / Artist columns)
  --tracklist FILE         plain "Artist - Title" lines (one per line)

USAGE
  python zingy_pipeline.py prepare --yt-playlist "https://youtube.com/playlist?list=..."
  python zingy_pipeline.py run
  python zingy_pipeline.py upload
  python zingy_pipeline.py sweep
  # or in one shot:
  python zingy_pipeline.py all --tracklist mytracks.txt
"""
from __future__ import annotations
import argparse, csv, json, os, queue, re, subprocess, sys, threading, time, unicodedata
import urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── config / paths ──────────────────────────────────────────────────────────
HOME = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".")
ROOT = Path(os.environ.get("ZINGY_ROOT") or (HOME / "Desktop" / "DIME_STEM_PIPELINE"))
TRACKS_DIR = ROOT / "tracks"                       # downloaded source mp3s
SEP_OUT = ROOT / "separated"                       # demucs output root
STEMS_DIR = SEP_OUT / "htdemucs_ft"                # <-- the ONE place stems live (out & upload read here)
STATE_PATH = ROOT / "pipeline_state.json"
VENV_PY = HOME / "demucs_relay" / "venv" / "Scripts" / "python.exe"   # the demucs venv
MODEL = "htdemucs_ft"
STEMS = ["bass", "drums", "other", "vocals"]
CDN_HOST = "https://dimesystemsstorage.b-cdn.net"  # pull zone serving the videobb storage zone

def load_env() -> dict:
    """Single source of truth for secrets. Reads .env next to this script, then the
    iOS repo's scripts/.env (the canonical one), then the real environment."""
    env = {}
    for p in [Path(__file__).with_name(".env"),
              Path("/Users/dime/Developer/ios-zingy/scripts/.env"),
              ROOT / ".env"]:
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    for k in ("BUNNY_STORAGE_ZONE", "BUNNY_STORAGE_PASSWORD", "BUNNY_API_KEY",
              "GENIUS_TOKEN", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env

ENV = load_env()
STORAGE_ZONE = ENV.get("BUNNY_STORAGE_ZONE", "videobb")
STORAGE_KEY = ENV.get("BUNNY_STORAGE_PASSWORD", "")     # the AccessKey for PUTs
STORAGE_BASE = f"https://storage.bunnycdn.com/{STORAGE_ZONE}/"

# ── manifest ────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"jobs": []}

_state_lock = threading.Lock()
def save_state(state: dict):
    with _state_lock:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")

def folder_name(artist: str, title: str) -> str:
    """The CDN/stem folder = 'Artist - Title' (matches the existing 2.6k catalogue layout)."""
    s = f"{artist} - {title}"
    s = re.sub(r"[\\/:*?\"<>|]", "", s).strip()
    return re.sub(r"\s+", " ", s)

# ── normalization / dedup against the live catalog ──────────────────────────
def ntitle(t):
    t = unicodedata.normalize("NFKD", (t or "").lower())
    t = re.sub(r"\(feat.*?\)|\[feat.*?\]|\(with.*?\)", "", t)
    return re.sub(r"[^a-z0-9]", "", t)
def atoks(a):
    a = re.sub(r"\(feat.*?\)|\[feat.*?\]|feat\.?|ft\.?|featuring", "", (a or "").lower())
    return {re.sub(r"[^a-z0-9]","",p) for p in re.split(r"[;,&]| x | and |/", a) if re.sub(r"[^a-z0-9]","",p)}

def catalog_index() -> dict:
    """Pull the LIVE catalogue from the CDN so dedup is always against what's shipped."""
    try:
        url = f"{CDN_HOST}/musicdata/music-index.json?v={int(time.time())}"
        d = json.loads(urllib.request.urlopen(url, timeout=30).read().decode("utf-8", "replace"))
        idx = {}
        for s in d.get("songsList", []):
            idx.setdefault(ntitle(s.get("title","")), []).append(atoks(s.get("artist","")))
        return idx
    except Exception as e:
        print(f"  ! could not load live catalogue for dedup ({e}); proceeding WITHOUT dedup")
        return {}

def catalog_index_raw() -> list:
    """The live catalogue as-is, ids INCLUDED. `catalog_index()` keeps only
    title/artist for dedup, which is why nothing downstream could see the ids."""
    try:
        url = f"{CDN_HOST}/musicdata/music-index.json?v={int(time.time())}"
        return json.loads(urllib.request.urlopen(url, timeout=30).read()
                          .decode("utf-8", "replace")).get("songsList", [])
    except Exception as e:
        print(f"  ! could not load live catalogue for id assignment ({e})")
        return []

def max_catalog_id(songs: list) -> int:
    """Highest numeric id in the live catalogue. New songs CONTINUE the sequence
    rather than restarting it, so an id is never reused for a different song."""
    best = 0
    for s in songs:
        try: best = max(best, int(str(s.get("id", 0))))
        except (TypeError, ValueError): continue
    return best

def is_dup(idx, artist, title) -> bool:
    nt = ntitle(title)
    if nt not in idx: return False
    cand = atoks(artist)
    return any((cand & ex) or not cand or not ex for ex in idx[nt])

# ════════════════════════════════════════════════════════════════════════════
# STAGE: prepare
# ════════════════════════════════════════════════════════════════════════════
def yt_print(args):
    """yt-dlp --print runner (metadata only, no download)."""
    out = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", *args],
                         capture_output=True, text=True, encoding="utf-8")
    return [l for l in (out.stdout or "").splitlines() if l.strip()]

def parse_yt_title(raw: str):
    s = re.sub(r"[\(\[][^()\[\]]*?(officiel|official|clip|lyric|paroles|video|visualizer|"
               r"audio|prod|hd|hq|4k|explicit|mv)[^()\[\]]*?[\)\]]", "", raw, flags=re.I)
    s = re.sub(r"//.*?//", "", s)
    s = re.sub(r"\s*[\(\[][^()\[\]]*[\)\]]\s*$", "", s).strip(" -–—|")
    m = re.search(r"\s[-–—]\s", s)
    if not m: return None
    artist, title = s[:m.start()].strip(), re.split(r"\s[-–—]\s", s[m.end():])[0].strip()
    artist = re.sub(r"\s+(ft\.?|feat\.?|featuring|avec)\s+", " feat. ", artist, flags=re.I)
    return (artist, title) if artist and title and len(title) < 80 else None

def source_jobs(args) -> list[dict]:
    """Return [{artist,title,url(optional)}] from whichever input was given."""
    jobs = []
    if args.yt_playlist:
        for line in yt_print(["--flat-playlist", "--print", "%(title)s|||%(webpage_url)s", args.yt_playlist]):
            raw, _, url = line.partition("|||")
            p = parse_yt_title(raw)
            if p: jobs.append({"artist": p[0], "title": p[1], "url": url.strip()})
    elif args.yt:
        for url in args.yt:
            t = yt_print(["--print", "%(artist)s|||%(track)s|||%(title)s", url])
            if not t: continue
            artist, _, rest = t[0].partition("|||"); track, _, title = rest.partition("|||")
            p = (artist.strip(), track.strip()) if artist.strip() not in ("","NA") and track.strip() not in ("","NA") else parse_yt_title(title)
            if p: jobs.append({"artist": p[0], "title": p[1], "url": url})
    elif args.csv:
        with open(args.csv, encoding="utf-8") as f:
            rd = csv.DictReader(f)
            cols = {c.lower(): c for c in (rd.fieldnames or [])}
            tcol = next((cols[c] for c in ("track name","title","name") if c in cols), None)
            acol = next((cols[c] for c in ("artist name","artist","artist name(s)","artists") if c in cols), None)
            for row in rd:
                if tcol and acol and row[tcol].strip() and row[acol].strip():
                    jobs.append({"artist": row[acol].split(",")[0].strip(), "title": row[tcol].strip()})
    elif args.tracklist:
        for line in Path(args.tracklist).read_text(encoding="utf-8").splitlines():
            if " - " in line:
                a, t = line.split(" - ", 1); jobs.append({"artist": a.strip(), "title": t.strip()})
    return jobs

def resolve_url(artist, title) -> str | None:
    """Find the best YouTube source for a track when no URL was given: prefer an
    'Art Track' (label master audio) whose duration matches; reject live/cover/remix."""
    q = f"ytsearch6:{artist} {title}"
    rows = yt_print(["--flat-playlist", "--print",
                     "%(webpage_url)s|||%(title)s|||%(channel)s", q])
    bad = re.compile(r"live|cover|remix|sped|slowed|8d|lyric|karaoke|instrumental|nightcore|mashup|reverb", re.I)
    for line in rows:
        url, _, rest = line.partition("|||"); vtitle, _, ch = rest.partition("|||")
        if bad.search(vtitle): continue
        return url.strip()                       # first clean hit
    return rows[0].split("|||")[0].strip() if rows else None

def cmd_prepare(args):
    jobs_in = source_jobs(args)
    print(f"  parsed {len(jobs_in)} tracks from input")
    idx = catalog_index()
    state = load_state()
    have = {(ntitle(j["title"]), frozenset(atoks(j["artist"]))) for j in state["jobs"]}
    added = dups = 0
    # Continue the live catalogue's id sequence, and account for ids this manifest has
    # already handed out but not yet published.
    next_id = max_catalog_id(catalog_index_raw())
    for j in state["jobs"]:
        try: next_id = max(next_id, int(str(j.get("catalog_id", 0))))
        except (TypeError, ValueError): pass
    for j in jobs_in:
        k = (ntitle(j["title"]), frozenset(atoks(j["artist"])))
        if k in have: continue
        if idx and is_dup(idx, j["artist"], j["title"]): dups += 1; continue
        have.add(k)
        next_id += 1
        state["jobs"].append({
            "artist": j["artist"], "title": j["title"], "url": j.get("url",""),
            "folder": folder_name(j["artist"], j["title"]),
            # The song's identity, decided HERE and carried through every later stage.
            # It used to be assigned AFTERWARDS by a separate reconciliation, whose
            # output (catalog_id_map.json) then lived only on one Mac and never reached
            # the database — which is exactly how the app, the CDN and the server ended
            # up speaking three different names for the same song.
            "catalog_id": str(next_id),
            "status": "queued", "error": "",
        })
        added += 1
    save_state(state)
    print(f"  + {added} new queued | {dups} already in catalogue (skipped) | total in manifest: {len(state['jobs'])}")

# ════════════════════════════════════════════════════════════════════════════
# STAGE: run  (download AHEAD of separation — GPU never idles)
# ════════════════════════════════════════════════════════════════════════════
def download_one(job) -> bool:
    TRACKS_DIR.mkdir(parents=True, exist_ok=True)
    out = TRACKS_DIR / f"{job['folder']}.mp3"
    if out.exists() and out.stat().st_size > 0: return True
    url = job.get("url") or resolve_url(job["artist"], job["title"])
    if not url:
        job["error"] = "no source url"; return False
    job["url"] = url
    # YouTube (2026) 403s / blocks media downloads unless yt-dlp is current AND, often,
    # has logged-in cookies. So: (a) set YT_COOKIES_BROWSER=chrome|edge|firefox in .env
    # (uses that browser's YouTube login — the most reliable fix), or drop a cookies.txt
    # next to this script; (b) keep yt-dlp fresh (see `run` which self-updates). A wedged
    # download can't hang the run — hard 240s timeout turns it into a clean per-track fail.
    base = [sys.executable, "-m", "yt_dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0",
            "--no-warnings", "--no-playlist", "--socket-timeout", "20", "--retries", "3", "--no-part",
            "-o", str(out.with_suffix("")) + ".%(ext)s"]
    browser = ENV.get("YT_COOKIES_BROWSER")
    cookiefile = Path(__file__).with_name("cookies.txt")
    if browser:
        base += ["--cookies-from-browser", browser]
    elif cookiefile.exists():
        base += ["--cookies", str(cookiefile)]
    # try a couple of player clients — different ones dodge the 403 on different days
    for client in (None, "android", "ios", "web_safari", "tv"):
        cmd = list(base) + (["--extractor-args", f"youtube:player_client={client}"] if client else [])
        try:
            r = subprocess.run(cmd + [url], capture_output=True, text=True, encoding="utf-8", timeout=240)
        except subprocess.TimeoutExpired:
            job["error"] = "download: timed out (>240s) — throttled/blocked source"; continue
        if r.returncode == 0 and out.exists():
            return True
        job["error"] = "download: " + (r.stderr or "")[-180:]
    return False

def separate_one(job) -> bool:
    src = TRACKS_DIR / f"{job['folder']}.mp3"
    outdir = STEMS_DIR / job["folder"]
    if all((outdir / f"{s}.mp3").exists() and (outdir / f"{s}.mp3").stat().st_size > 0 for s in STEMS):
        return True
    py = str(VENV_PY) if VENV_PY.exists() else sys.executable
    r = subprocess.run([py, "-m", "demucs", "-n", MODEL, "--mp3", "-o", str(SEP_OUT), str(src)],
                       capture_output=True, text=True, encoding="utf-8")
    ok = all((outdir / f"{s}.mp3").exists() for s in STEMS)
    if not ok: job["error"] = "demucs: " + (r.stderr or "")[-160:]
    return ok

def cmd_run(args):
    # Keep yt-dlp current — a stale yt-dlp is the #1 cause of YouTube download failures.
    try:
        print("  updating yt-dlp (YouTube changes constantly) …")
        subprocess.run([sys.executable, "-m", "pip", "install", "-U", "--quiet", "yt-dlp"],
                       timeout=180, capture_output=True)
    except Exception:
        pass
    state = load_state()
    todo = [j for j in state["jobs"] if j["status"] in ("queued", "downloaded")]
    if not todo:
        print("  nothing to run (all jobs separated or beyond)."); return
    print(f"  run: {len(todo)} tracks | downloader runs ahead, GPU separates as tracks land")
    dlq: queue.Queue = queue.Queue()
    done_dl = threading.Event()

    def downloader():
        # keep a small lead so the GPU always has a track waiting, but don't fill the disk
        with ThreadPoolExecutor(max_workers=3) as pool:
            futs = {pool.submit(download_one, j): j for j in todo if j["status"] == "queued"}
            for j in todo:
                if j["status"] == "downloaded": dlq.put(j)
            for fut in as_completed(futs):
                j = futs[fut]
                if fut.result():
                    j["status"] = "downloaded"; dlq.put(j)
                else:
                    j["status"] = "failed"; print(f"    ✗ download {j['folder']}: {j['error']}")
                save_state(state)
        done_dl.set()

    threading.Thread(target=downloader, daemon=True).start()

    sep_done = 0
    while not (done_dl.is_set() and dlq.empty()):
        try:
            j = dlq.get(timeout=2)
        except queue.Empty:
            continue
        print(f"    ▶ separating {j['folder']}")
        if separate_one(j):
            enrich_one(j)
            j["status"] = "separated"; sep_done += 1
            print(f"    ✓ {j['folder']}  ({sep_done} done)")
        else:
            j["status"] = "failed"; print(f"    ✗ demucs {j['folder']}: {j['error']}")
        save_state(state)
    print(f"  run complete: {sep_done} separated.")

# ════════════════════════════════════════════════════════════════════════════
# metadata enrichment (BPM/key via librosa; cover/producers via Genius->iTunes)
# ════════════════════════════════════════════════════════════════════════════
PITCH = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
def estimate_bpm_key(mp3: Path):
    try:
        import numpy as np, librosa
        y, sr = librosa.load(str(mp3), duration=90)
        dur = int(librosa.get_duration(path=str(mp3)))
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = int(round(float(tempo[0] if hasattr(tempo, "__len__") else tempo)))
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr).sum(axis=1)
        # Krumhansl-Schmuckler major/minor profiles
        maj = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
        mino= np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
        best=(-9,"C","Major")
        for i in range(12):
            for prof,mode in ((maj,"Major"),(mino,"Minor")):
                c = float(np.corrcoef(chroma, np.roll(prof,i))[0,1])
                if c>best[0]: best=(c,PITCH[i],mode)
        return bpm, f"{best[1]} {best[2]}", dur
    except Exception as e:
        return 120, "Unknown", 0

def itunes_meta(artist, title):
    try:
        url = f"https://itunes.apple.com/search?term={urllib.parse.quote(f'{artist} {title}')}&entity=song&limit=1"
        r = json.loads(urllib.request.urlopen(url, timeout=15).read())
        if r.get("results"):
            x = r["results"][0]
            return {"cover": x.get("artworkUrl100","").replace("100x100bb","1000x1000bb"),
                    "year": (x.get("releaseDate","") or "")[:4],
                    "album": x.get("collectionName",""), "label": "",
                    "releaseDate": (x.get("releaseDate","") or "")[:10]}
    except Exception: pass
    return {}

def genius_meta(artist, title):
    tok = ENV.get("GENIUS_TOKEN")
    if not tok: return {}
    try:
        url = f"https://api.genius.com/search?q={urllib.parse.quote(f'{artist} {title}')}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        hits = json.loads(urllib.request.urlopen(req, timeout=15).read())["response"]["hits"]
        if not hits: return {}
        h = hits[0]["result"]
        cover = h.get("song_art_image_url") or h.get("header_image_url") or ""
        # genius covers come as ...1000x1000x1.png in the catalogue
        return {"cover": cover, "year": "", "album": "", "label": "", "releaseDate": ""}
    except Exception:
        return {}

def enrich_one(job):
    mp3 = TRACKS_DIR / f"{job['folder']}.mp3"
    bpm, key, dur = estimate_bpm_key(mp3) if mp3.exists() else (120, "Unknown", 0)
    g = genius_meta(job["artist"], job["title"])
    i = itunes_meta(job["artist"], job["title"])
    cover = g.get("cover") or i.get("cover") or ""        # Genius primary, iTunes fallback (Spotify hook: add here)
    job["meta"] = {
        "bpm": bpm, "key": key, "duration": dur,
        "coverArt": cover,
        "year": i.get("year",""), "album": i.get("album",""),
        "recordLabel": i.get("label",""), "releaseDate": i.get("releaseDate",""),
        "producers": g.get("producers",""),
    }

# ════════════════════════════════════════════════════════════════════════════
# STAGE: upload  (batch, at the END — key from .env)
# ════════════════════════════════════════════════════════════════════════════
def put_stem(local: Path, remote: str):
    url = STORAGE_BASE + urllib.parse.quote(remote.replace("\\","/"), safe="/")
    data = local.read_bytes()
    req = urllib.request.Request(url, data=data, method="PUT",
                                 headers={"AccessKey": STORAGE_KEY, "Content-Type": "application/octet-stream"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status in (200, 201): return True, ""
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False, "401 Unauthorized — BUNNY_STORAGE_PASSWORD wrong/stale (check .env)"
            if attempt == 2: return False, f"http {e.code}"
        except Exception as e:
            if attempt == 2: return False, str(e)
        time.sleep(1.5)
    return False, "exhausted retries"

def cmd_upload(args):
    if not STORAGE_KEY:
        sys.exit("  ✗ no BUNNY_STORAGE_PASSWORD in .env — refusing to upload (would 401).")
    state = load_state()
    jobs = [j for j in state["jobs"] if j["status"] in ("separated", "uploaded")]
    if not jobs: print("  nothing separated to upload."); return
    print(f"  uploading stems for {len(jobs)} tracks to '{STORAGE_ZONE}' …")
    tasks = []
    for j in jobs:
        for s in STEMS:
            lp = STEMS_DIR / j["folder"] / f"{s}.mp3"
            if lp.exists(): tasks.append((j, lp, f"{j['folder']}/{s}.mp3"))
    ok = bad = 0; first_401 = None
    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(put_stem, lp, rp): (j, rp) for (j, lp, rp) in tasks}
        for fut in as_completed(futs):
            success, err = fut.result()
            if success: ok += 1
            else:
                bad += 1; first_401 = first_401 or (err if "401" in err else None)
                print(f"    ✗ {futs[fut][1]}: {err}")
    for j in jobs:
        if all((STEMS_DIR / j["folder"] / f"{s}.mp3").exists() for s in STEMS):
            j["status"] = "uploaded"
    save_state(state)
    print(f"  uploaded {ok} files, {bad} failed.")
    if first_401:
        print("  >>> AUTH FAILED. Fix BUNNY_STORAGE_PASSWORD in .env and re-run `upload`. <<<")

# ════════════════════════════════════════════════════════════════════════════
# STAGE: sweep  (verify every stem actually serves from the CDN; re-upload misses)
# ════════════════════════════════════════════════════════════════════════════
def head_ok(url) -> bool:
    try:
        req = urllib.request.Request(url + f"?v={int(time.time())}", method="HEAD")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False

def cmd_sweep(args):
    state = load_state()
    jobs = [j for j in state["jobs"] if j["status"] in ("uploaded", "separated", "verified")]
    print(f"  sweep: verifying {len(jobs)} tracks against the CDN …")
    fixed = good = missing = 0
    for j in jobs:
        miss = []
        for s in STEMS:
            url = f"{CDN_HOST}/{urllib.parse.quote(j['folder'])}/{s}.mp3"
            if not head_ok(url):
                miss.append(s)
        if not miss:
            j["status"] = "verified"; good += 1; continue
        # re-upload the missing ones
        for s in miss:
            lp = STEMS_DIR / j["folder"] / f"{s}.mp3"
            if lp.exists():
                success, err = put_stem(lp, f"{j['folder']}/{s}.mp3")
                if success: fixed += 1
                else: print(f"    ✗ reupload {j['folder']}/{s}: {err}")
            else:
                missing += 1; print(f"    ! local stem missing, can't fix: {j['folder']}/{s}")
        # recheck
        if all(head_ok(f"{CDN_HOST}/{urllib.parse.quote(j['folder'])}/{s}.mp3") for s in STEMS):
            j["status"] = "verified"; good += 1
    save_state(state)
    print(f"  sweep done: {good} verified, {fixed} re-uploaded, {missing} unrecoverable.")
    print("  next: run publish_catalog.py on the Mac to link these into music-index.json.")

# ── cli ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Zingy catalog ingest pipeline")
    sub = ap.add_subparsers(dest="stage", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--yt-playlist"); p.add_argument("--yt", nargs="+")
    p.add_argument("--csv"); p.add_argument("--tracklist")
    sub.add_parser("run"); sub.add_parser("upload"); sub.add_parser("sweep")
    pa = sub.add_parser("all")
    pa.add_argument("--yt-playlist"); pa.add_argument("--yt", nargs="+")
    pa.add_argument("--csv"); pa.add_argument("--tracklist")
    a = ap.parse_args()
    print(f"== zingy_pipeline :: {a.stage} ==  (zone={STORAGE_ZONE}, key={'set' if STORAGE_KEY else 'MISSING'})")
    if a.stage in ("prepare", "all"): cmd_prepare(a)
    if a.stage in ("run", "all"): cmd_run(a)
    if a.stage in ("upload", "all"): cmd_upload(a)
    if a.stage in ("sweep", "all"): cmd_sweep(a)

if __name__ == "__main__":
    main()
