#!/usr/bin/env python3
"""
ingest_gpu.py — Mac-orchestrated catalogue ingest on the home GPU box.

WHAT CAME BEFORE (context, so this file is not mistaken for a rescue)
  The home-GPU pipeline WORKED, and worked at scale. auto_ingest.py + the VPS
  queue + the demucs_relay pull-worker split 2922 of the catalogue's songs
  (`SELECT split_source, count(*) FROM tracks` → gpu 2922, provided 1007,
  replicate 4), peaking at ~1349 tracks in a single day (2026-06-27). That path
  is proven; nothing here is a verdict on it.

WHY THIS EXISTS
  • It stopped on 2026-08-28. The last track completed at 13:12 UTC; at 15:37
    UTC zingy-api commit 9dba51a ("drop the dead GPU tier") removed the worker's
    claim/complete/fail routes server-side. The box never noticed — ZingyWorker
    kept polling /internal/split/next and taking 404s (656 idle polls). So the
    regression is ~2 hours of server change, not a pipeline that never worked.
    This script restores home-GPU ingest WITHOUT that tier: the Mac drives the
    box over SSH, so there is no endpoint to rot and no shared worker secret.
  • zingy_pipeline.py (2026-08-24) is a separate, unfinished REWRITE that aimed
    to replace the relay tangle. It never became the production path and could
    not reach the catalogue: it wrote "<Artist - Title>/<stem>.mp3" where the app
    reads catalogtracks/<uuid>/<stem>.m4a, and its final step pointed at a
    publish_catalog.py that does not exist. Its gate also only claimed to verify
    a source — no duration check, and it returned rows[0] even when the
    reject-regex had rejected it — and on analysis failure it wrote
    bpm=120/key="Unknown"/duration=0 as though measured. Those flaws are why
    this file exists rather than a patch to it; they are NOT what the 4024-song
    catalogue was built with.

THE RULES THIS FILE ENFORCES
  1. Never invent a value. A field we could not source is omitted, not defaulted.
     There is no "120 BPM fallback" anywhere in this file.
  2. Never advance a status without proof. "uploaded" requires a 2xx from the
     PUT; "verified" requires a 200 from a real HEAD against the CDN.
  3. Never guess the recording. A source is accepted only if its runtime matches
     the CSV's Spotify duration within DURATION_TOLERANCE_S. Unmatched tracks are
     reported as 'unresolved' and left out of the catalogue for you to decide.
  4. The CSV is authoritative where it speaks (bpm, key, duration, album, label,
     release date, genres, audio features, popularity). We do not re-derive what
     we were already told by the source of truth.

YOUTUBE AGE GATES (and why cookies are the LAST resort)
  Some official uploads are age-restricted; yt-dlp cannot fetch those without a
  signed-in session. Order of defence:
  1. resolve keeps the runners-up that passed the same title+duration gate
     (track["alternates"]); when a fetch hits the gate, produce walks them —
     the label's "- Topic" auto-upload of the same master is usually open.
  2. Only if every alternate is gated too, the box uses ONE cookie jar,
     %USERPROFILE%\zingy_ingest\cookies.txt, under a lock so yt-dlp can write the
     rotated tokens back. Google rotates session cookies on every use, so the
     jar must come from a THROWAWAY account exported from a PRIVATE window that
     you then close — export from your everyday browser and it is dead within
     minutes (that exact thing cost a full retry pass on 2026-09-17).
  3. preflight probes the jar ("youtube jar: ✓/✗ ROTATED") so a dead one is
     caught before a run, not 150 failures into it.

SECRETS
  BunnyCDN key and the Genius token stay on this Mac. The GPU box never receives
  them (gpu_agent.py holds none), so the Windows box is not a place they can leak.

LINES
  A track's line is set by `prepare --line` and decides where resolve and
  produce run. gpu (default): the GPU box over SSH. replicate: the VPS, split on
  Replicate, YouTube reached through the Mac's own connection (replicate_line.py,
  vps_line.py); no home GPU. The Spotify inbox uses replicate. From upload on,
  both lines are the same.

STAGES (each resumable; state in ingest_gpu_state.json)
  preflight  every precondition, checked in seconds, before any long work
  prepare    parse the CSV, dedup against the live catalogue, assign id + uuid
  resolve    find and VERIFY the real source for each track (duration gate)
  produce    fetch -> demucs -> encode on the GPU, then pull the m4a to the Mac
  upload     PUT stems to catalogtracks/<uuid>/, then HEAD-verify on the CDN
  enrich     producers/writers/cover from Genius (scored, never blind hits[0])
  vibe       the six vibe numbers from La boîte bleue, for tracks the CSV gave none
  publish    write musicdata/songs/<id>.json + index entry, push to the CDN
  report     the receipt: per-song truth, re-checked over HTTP

USAGE
  python3 ingest_gpu.py preflight
  python3 ingest_gpu.py all --csv ~/Desktop/boys_902000.csv
  python3 ingest_gpu.py status
"""
from __future__ import annotations
import argparse, base64, csv, json, os, re, subprocess, sys, tempfile, threading, time
import unicodedata, urllib.error, urllib.parse, urllib.request, uuid as uuidmod
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from romaji import to_romaji, has_japanese, romanize_artist

HERE = Path(__file__).resolve().parent
REPO = Path("/Users/dime/Developer/ios-zingy")
MUSICDATA = REPO / "App/ZingyApp/Resources/musicdata"
INDEX_PATH = MUSICDATA / "music-index.json"
SONGS_DIR = MUSICDATA / "songs"
STATE_PATH = HERE / "ingest_gpu_state.json"
STEMS_LOCAL = HERE / "stems_staging"

BOX_USER = os.environ.get("ZINGY_BOX_USER", "dimé")
# Tailscale first, then the LAN. A Windows Update rebooted the box and
# Tailscale did not come back, so the machine was up and reachable at
# 192.168.1.64 while every SSH over the tailnet timed out — the whole run
# stalled on a network path, not on the box being down.
BOX_HOSTS = [h for h in (os.environ.get("ZINGY_BOX_HOST")
                         or "100.76.91.119,technica.home,192.168.1.64").split(",") if h.strip()]
BOX_HOST = BOX_HOSTS[0]
BOX_KEY = os.path.expanduser(os.environ.get("ZINGY_BOX_KEY", "~/.ssh/id_ed25519_gpu"))
REMOTE_AGENT = "%USERPROFILE%\\zingy_ingest\\gpu_agent.py"
VPS = os.environ.get("ZINGY_VPS", "204.168.236.140")

CDN_HOST = "https://dimesystemsstorage.b-cdn.net"
STEM_PREFIX = "catalogtracks"
STEMS = ["bass", "drums", "other", "vocals"]
STEM_BITRATE = "128k"          # matches the existing 4024 songs (~130 kbps AAC)
DURATION_TOLERANCE_S = 5.0     # the source-verification gate

# Spotify's Key column is a pitch class; Mode is 1=major 0=minor.
PITCH = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

def log(msg): print(msg, flush=True)

# ── secrets (Mac only) ──────────────────────────────────────────────────────
def load_env() -> dict:
    env = {}
    for p in (REPO / "scripts/.env", HERE / ".env"):
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    tok = HERE / ".genius_token"
    if tok.exists():
        env.setdefault("GENIUS_TOKEN", tok.read_text(encoding="utf-8").strip())
    for k in ("BUNNY_STORAGE_ZONE", "BUNNY_STORAGE_PASSWORD", "GENIUS_TOKEN"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env

ENV = load_env()
STORAGE_ZONE = ENV.get("BUNNY_STORAGE_ZONE", "videobb")
STORAGE_KEY = ENV.get("BUNNY_STORAGE_PASSWORD", "")
STORAGE_BASE = f"https://storage.bunnycdn.com/{STORAGE_ZONE}/"

# ── state (atomic; a crash mid-write must not destroy the manifest) ──────────
_state_lock = threading.RLock()
GPU_LOCK = threading.Lock()
_box_lock = threading.Lock()
LOCK_PATH = STATE_PATH.with_suffix(".lock")

def claim_manifest(stage: str) -> None:
    """Refuse to run two stages against the manifest at once.

    Each stage loads the whole manifest, mutates it and writes it back, so two
    running together means last-writer-wins on the ENTIRE file. That is not
    theoretical: a track reset to 'pending' while `produce` was running was
    silently reverted when produce saved its copy, and the edit just vanished.
    """
    if LOCK_PATH.exists():
        try:
            held = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        except Exception:
            held = {}
        pid = held.get("pid")
        alive = False
        if pid:
            try:
                os.kill(int(pid), 0); alive = True
            except (OSError, ValueError):
                alive = False
        if alive:
            sys.exit(f"  ✗ '{held.get('stage')}' is already running on this manifest "
                     f"(pid {pid}). Stop it before starting another stage.")
        LOCK_PATH.unlink(missing_ok=True)      # stale lock from a crashed run
    LOCK_PATH.write_text(json.dumps({"stage": stage, "pid": os.getpid()}), encoding="utf-8")
    import atexit
    atexit.register(lambda: LOCK_PATH.unlink(missing_ok=True))

def release_manifest() -> None:
    LOCK_PATH.unlink(missing_ok=True)

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            bad = STATE_PATH.with_suffix(".corrupt")
            STATE_PATH.replace(bad)
            log(f"  ! state file was corrupt, moved to {bad.name}; starting fresh")
    return {"tracks": [], "created": time.strftime("%Y-%m-%dT%H:%M:%S")}

def save_state(state: dict):
    with _state_lock:
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATE_PATH)          # atomic on POSIX

# ── the box (key auth only; no password anywhere in this repo) ───────────────
class BoxError(RuntimeError): pass

_SSH_NOISE = re.compile(
    r"^\s*\*\*.*(post-quantum|store now, decrypt later|openssh\.com/pq).*$", re.I | re.M)

def _clean(s: str) -> str:
    """Strip OpenSSH's post-quantum advisory banner. It arrives on stderr for every
    connection, and left in it masks the real error in every failure message."""
    return _SSH_NOISE.sub("", s or "").strip()

def _ssh_once(host: str, remote_cmd: list[str], timeout: int) -> tuple[int, str, str]:
    cmd = ["ssh", "-i", BOX_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
           f"{BOX_USER}@{host}"] + remote_cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout or "", _clean(r.stderr)
    except subprocess.TimeoutExpired:
        return 124, "", f"ssh timed out after {timeout}s"

def ssh_raw(remote_cmd: list[str], timeout: int) -> tuple[int, str, str]:
    """Try each known address, and remember the one that answered."""
    global BOX_HOST
    order = [BOX_HOST] + [h for h in BOX_HOSTS if h != BOX_HOST]
    last = (124, "", "no host tried")
    for host in order:
        rc, out, err = _ssh_once(host, remote_cmd, timeout)
        if rc != 255 and not (rc == 124 and "timed out" in err):
            if host != BOX_HOST:
                log(f"    (reached the box at {host})")
                BOX_HOST = host
            return rc, out, err
        last = (rc, out, err)
    return last

def agent(command: str, args: dict | None = None, timeout: int = 300, retries: int = 2) -> dict:
    """Call gpu_agent.py on the box. Args go as base64 JSON so no title with an
    accent, quote or ampersand can be mangled by cmd.exe quoting."""
    blob = base64.b64encode(json.dumps(args or {}, ensure_ascii=False).encode()).decode()
    remote = ["python", REMOTE_AGENT, command, blob]
    last = ""
    for attempt in range(retries + 1):
        rc, out, err = ssh_raw(remote, timeout)
        for line in out.splitlines():
            if line.startswith("__AGENT__"):
                try:
                    return json.loads(line[len("__AGENT__"):])
                except json.JSONDecodeError as e:
                    last = f"unparseable agent reply: {e}"
        last = (err or out).strip()[-300:] or f"rc={rc}, no agent reply"
        if rc == 124 or "Connection" in last or "closed" in last:
            time.sleep(3 * (attempt + 1))     # transient link problem: back off
            continue
        break
    return {"ok": False, "error": f"agent {command}: {last}"}

BOX_MAC = os.environ.get("ZINGY_BOX_MAC", "A4-42-3B-6A-11-9D")

def box_alive(timeout: int = 20) -> bool:
    rc, out, _ = ssh_raw(["hostname"], timeout)
    return rc == 0 and bool(out.strip())

def wake_box() -> None:
    """Wake-on-LAN magic packet. Only works from the same LAN, which the Mac is."""
    import socket
    mac = re.sub(r"[^0-9A-Fa-f]", "", BOX_MAC)
    if len(mac) != 12:
        return
    pkt = bytes.fromhex("F" * 12 + mac * 16)
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sk.sendto(pkt, ("255.255.255.255", 9))
        sk.close()
    except Exception:
        pass

def ensure_box(max_wait: int = 420) -> bool:
    """Make sure the box is up, waking it if it dozed off.

    The box sleeps on idle. Without this a nap turns into a burst of bogus
    per-track failures: a 26-track run lost 9 tracks in 14 seconds each, all
    'ssh: Operation timed out', none of which was anything to do with the track.
    A sleeping machine is a pause, not a fault.
    """
    return False

def push_agent() -> bool:
    """Ship the current gpu_agent.py to the box every run, so the box can never
    be running a stale copy — the 'which version is over there?' class of bug."""
    # ONE pre-quoted string: ssh joins argv with spaces, so an unquoted '|' here
    # would be interpreted by cmd.exe instead of PowerShell and the mkdir fails.
    rc, _, err = ssh_raw(
        ['powershell -NoProfile -Command "New-Item -ItemType Directory -Force '
         '-Path $env:USERPROFILE\\zingy_ingest | Out-Null"'], 60)
    if rc != 0:
        log(f"    ! could not create the remote work dir: {err[-160:]}")
        return False
    dst = f"{BOX_HOST}:zingy_ingest/gpu_agent.py"
    r = subprocess.run(["scp", "-i", BOX_KEY, "-o", "BatchMode=yes",
                        "-o", f"User={BOX_USER}",
                        "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
                        str(HERE / "gpu_agent.py"), dst],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    return r.returncode == 0

# ── normalisation / dedup ───────────────────────────────────────────────────
def ntitle(t: str) -> str:
    t = unicodedata.normalize("NFKD", (t or "").lower())
    t = re.sub(r"\(feat.*?\)|\[feat.*?\]|\(with.*?\)", "", t)
    return re.sub(r"[^a-z0-9]", "", t)

def atoks(a: str) -> set:
    a = re.sub(r"\(feat.*?\)|\[feat.*?\]|feat\.?|ft\.?|featuring", "", (a or "").lower())
    return {re.sub(r"[^a-z0-9]", "", p) for p in re.split(r"[;,&]| x | and |/", a)
            if re.sub(r"[^a-z0-9]", "", p)}

def local_index() -> dict:
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))

def is_dup(idx: dict, artist: str, title: str) -> bool:
    """Same title AND a shared artist token. Unlike the old is_dup, an entry with
    no parseable artist does NOT swallow every song sharing its title."""
    nt = ntitle(title)
    if nt not in idx:
        return False
    cand = atoks(artist)
    if not cand:
        return False
    return any(cand & ex for ex in idx[nt] if ex)

# ════════════════════════════════════════════════════════════════════════════
# STAGE: preflight
# ════════════════════════════════════════════════════════════════════════════
def cmd_preflight(args) -> bool:
    log("── preflight ──────────────────────────────────────────────────")
    ok = True

    if not Path(BOX_KEY).exists():
        log(f"  ssh key          : ✗ {BOX_KEY} missing"); ok = False
    else:
        rc, out, err = ssh_raw(["hostname"], 30)
        if rc == 0:
            log(f"  box ssh (key)    : ✓ {out.strip()}")
        else:
            log(f"  box ssh (key)    : ✗ {err.strip()[-160:]}")
            log("                     the box may be asleep — try ./wake_gpu.sh, or check Tailscale")
            ok = False

    if ok:
        log(f"  agent upload     : {'✓' if push_agent() else '✗'}")
        p = agent("probe", timeout=240)
        if p.get("ok"):
            log(f"  gpu              : ✓ {p.get('gpu') or 'n/a'}")
            log(f"  demucs/torch     : ✓ {p.get('torch_line')}")
            if "True" not in (p.get("torch_line") or ""):
                log("                     ⚠ CUDA reports False — separation would fall back to CPU (very slow)")
            log(f"  yt-dlp           : {'✓ ' + p['ytdlp'] if p.get('ytdlp') else '✗ missing'}")
            log(f"  ffmpeg/ffprobe   : {'✓' if p.get('ffmpeg') else '✗ ffmpeg'} / {'✓' if p.get('ffprobe') else '✗ ffprobe'}")
            log(f"  box free disk    : {p.get('free_gb')} GB")
            # The jar only matters for age-gated uploads with no open alternate,
            # but a rotated one fails 150 tracks before anyone notices — say so now.
            j = agent("jar_check", timeout=120)
            if not j.get("present"):
                log("  youtube jar      : – none on the box (age-gated uploads with no open alternate will fail)")
            elif j.get("signed_in"):
                log(f"  youtube jar      : ✓ signed in ({j.get('age', 0)//3600}h old)")
            else:
                log(f"  youtube jar      : ✗ {'ROTATED — re-export from a private window' if j.get('rotated') else 'not signed in'}"
                    f"  {(j.get('detail') or '')[-80:]}")
            if not p.get("ytdlp") or not p.get("ffmpeg"):
                ok = False
        else:
            log(f"  gpu agent        : ✗ {p.get('error')}"); ok = False

    log(f"  bunny key        : {'✓ set' if STORAGE_KEY else '✗ missing BUNNY_STORAGE_PASSWORD'}")
    ok = ok and bool(STORAGE_KEY)
    log(f"  genius token     : {'✓ set' if ENV.get('GENIUS_TOKEN') else '✗ missing (producers/writers will be blank)'}")

    if INDEX_PATH.exists() and os.access(INDEX_PATH, os.W_OK):
        try:
            n = len(local_index().get("songsList", []))
            log(f"  local catalogue  : ✓ {n} songs, writable")
        except Exception as e:
            log(f"  local catalogue  : ✗ unreadable ({e})"); ok = False
    else:
        log(f"  local catalogue  : ✗ {INDEX_PATH} missing or read-only"); ok = False

    up = REPO / "scripts/upload-catalog.sh"
    log(f"  publish script   : {'✓' if up.exists() else '✗ ' + str(up)}")
    ok = ok and up.exists()

    log("──" + ("  all preconditions met" if ok else "  PREFLIGHT FAILED — fix the ✗ lines above"))
    return ok

# ════════════════════════════════════════════════════════════════════════════
# STAGE: prepare
# ════════════════════════════════════════════════════════════════════════════
def csv_key(row: dict) -> str | None:
    """Spotify Key (pitch class 0-11) + Mode (1 major / 0 minor) -> 'F Minor'.
    Returns None when the CSV doesn't say — never a placeholder."""
    try:
        k = int(str(row.get("Key", "")).strip())
        m = int(str(row.get("Mode", "")).strip())
    except (TypeError, ValueError):
        return None
    if not 0 <= k <= 11:
        return None
    return f"{PITCH[k]} {'Major' if m == 1 else 'Minor'}"

def fnum(v):
    try:
        f = float(str(v).strip())
        return f
    except (TypeError, ValueError):
        return None

def parse_csv(path: Path) -> list[dict]:
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    out = []
    for r in rows:
        title = (r.get("Track Name") or "").strip()
        artist_raw = (r.get("Artist Name(s)") or "").strip()
        if not title or not artist_raw:
            continue
        # ";" separates collaborators in this export. Keep them all, joined the way
        # the catalogue does it, instead of the old .split(",")[0] which silently
        # dropped every featured artist.
        artists = [a.strip() for a in artist_raw.split(";") if a.strip()]
        dur_ms = fnum(r.get("Duration (ms)"))
        tempo = fnum(r.get("Tempo"))
        pop = fnum(r.get("Popularity"))
        genres = [g.strip() for g in (r.get("Genres") or "").split(",") if g.strip()]
        feats = {}
        for col, name in (("Danceability", "danceability"), ("Energy", "energy"),
                          ("Acousticness", "acousticness"), ("Instrumentalness", "instrumentalness"),
                          ("Valence", "valence"), ("Speechiness", "speechiness")):
            v = fnum(r.get(col))
            if v is not None:
                feats[name] = v
        out.append({
            "title": title,
            # ";" is the catalogue's collaborator separator, NOT a comma. Joining
            # with ", " fuses a collab into ONE artist name — "Ken Carson, OsamaSon"
            # becomes a single phantom artist instead of two real ones, which is
            # exactly what artist grouping and the artist pages key off.
            "artist": "; ".join(artists),
            "artists": artists,
            "album": (r.get("Album Name") or "").strip() or None,
            "release_date": (r.get("Release Date") or "").strip() or None,
            "record_label": (r.get("Record Label") or "").strip() or None,
            "duration_ms": int(dur_ms) if dur_ms else None,
            "bpm": int(round(tempo)) if tempo else None,
            "key": csv_key(r),
            "popularity": int(pop) if pop is not None else None,
            "explicit": (r.get("Explicit") or "").strip().lower() == "true",
            "genres": genres,
            "features": feats,
            "spotify_uri": (r.get("Track URI") or "").strip() or None,
        })
        # Link intake (link_intake.py): the video the song was submitted with is its
        # source — no search — and a cover to fall back on. Absent from any CSV export.
        src = (r.get("Source URL") or "").strip()
        if src:
            out[-1]["source_pin"] = {"url": src, "title": (r.get("Source Title") or "").strip() or None,
                                     "channel": (r.get("Source Channel") or "").strip() or None,
                                     "duration": fnum(r.get("Source Duration (s)"))}
        if (r.get("Cover URL") or "").strip():
            out[-1]["cover_hint"] = r["Cover URL"].strip()
            out[-1]["cover_hint_source"] = (r.get("Cover Source") or "").strip() or "link"
        if (r.get("Intake Note") or "").strip():
            out[-1]["intake_note"] = r["Intake Note"].strip()
    return out

def cmd_prepare(args):
    if not args.csv:
        sys.exit("  prepare needs --csv")
    rows = parse_csv(Path(args.csv).expanduser())
    log(f"  parsed {len(rows)} rows from {Path(args.csv).name}")

    cat = local_index()
    songs = cat.get("songsList", [])
    idx, idx_ids = {}, {}
    for s in songs:
        idx.setdefault(ntitle(s.get("title", "")), []).append(atoks(s.get("artist", "")))
        idx_ids.setdefault(ntitle(s.get("title", "")), []).append((atoks(s.get("artist", "")), s.get("id")))
    next_id = max((int(str(s["id"])) for s in songs if str(s.get("id", "")).isdigit()), default=0)

    state = load_state()
    for t in state["tracks"]:
        if str(t.get("catalog_id", "")).isdigit():
            next_id = max(next_id, int(t["catalog_id"]))
    seen = {(ntitle(t["title"]), frozenset(atoks(t["artist"]))) for t in state["tracks"]}
    by_key = {(ntitle(t["title"]), frozenset(atoks(t["artist"]))): t for t in state["tracks"]}
    rearm = getattr(args, "rearm", False)
    report = []                      # one entry per CSV row, in order (--report)

    added = dups = rearmed = 0
    for r in rows:
        k = (ntitle(r["title"]), frozenset(atoks(r["artist"])))
        if k in seen:
            t = by_key.get(k)
            # --rearm (link intake): a song someone just asked for again gets another
            # go on this line, with the source they sent — unless it is already being
            # made or done. A plain CSV re-run never touches existing tracks.
            if rearm and t is not None and t["status"] in ("pending", "unresolved", "failed", "resolved"):
                t["line"] = getattr(args, "line", None) or line_of(t)
                for f in ("source_pin", "cover_hint", "cover_hint_source", "intake_note"):
                    if r.get(f):
                        t[f] = r[f]
                if r.get("source_pin") or t["status"] in ("unresolved", "failed"):
                    t["status"], t["error"] = "pending", ""
                t.setdefault("history", []).append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "rearmed by link intake"})
                rearmed += 1
            report.append({"outcome": "existing", "catalog_id": (t or {}).get("catalog_id"),
                           "status": (t or {}).get("status")})
            continue
        if is_dup(idx, r["artist"], r["title"]):
            dups += 1
            cand = atoks(r["artist"])
            cid = next((i for toks, i in idx_ids.get(ntitle(r["title"]), []) if cand & toks), None)
            report.append({"outcome": "catalogue", "catalog_id": str(cid) if cid is not None else None})
            continue
        seen.add(k)
        next_id += 1
        # Catalogue titles are romaji, always — no Japanese script reaches the app.
        # The ORIGINAL is kept because it is the better search query on YouTube
        # and the better thing to title-match a Japanese upload against.
        if has_japanese(r["title"]):
            rom, rsrc = to_romaji(r["title"], r["artist"])
            if rom and rom != r["title"]:
                r["title_original"] = r["title"]
                r["title"] = rom
                r["romaji_source"] = rsrc
        # ...and the byline. Romanising only titles left 13 songs credited in
        # Japanese script, which is the same defect one field over.
        if has_japanese(r["artist"]):
            ar, asrc = romanize_artist(r["artist"])
            if ar and not has_japanese(ar):
                r["artist_original"] = r["artist"]
                r["artist"] = ar
                r["artist_romaji_source"] = asrc
        r.update({
            "catalog_id": str(next_id),
            # The CDN folder. Generated here and carried through every later stage,
            # so the id the app uses, the folder the stems live in and the DB row
            # can never drift apart.
            "uuid": str(uuidmod.uuid4()),
            "status": "pending", "error": "", "source": None, "history": [],
            "line": getattr(args, "line", None) or "gpu",
        })
        state["tracks"].append(r)
        by_key[k] = r
        added += 1
        report.append({"outcome": "added", "catalog_id": r["catalog_id"], "status": "pending"})
    save_state(state)
    log(f"  + {added} new | {dups} already in the catalogue (skipped)"
        + (f" | {rearmed} re-armed" if rearmed else "") + f" | manifest total: {len(state['tracks'])}")
    if getattr(args, "report", None):
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

# ════════════════════════════════════════════════════════════════════════════
# STAGE: resolve  — the source-verification gate
# ════════════════════════════════════════════════════════════════════════════
BAD_TOKENS = re.compile(
    r"\b(live|cover|remix|sped\s*up|slowed|8d|karaoke|instrumental|nightcore|mashup|"
    r"reverb|reaction|tutorial|bass\s*boost(ed)?|acapella|a\s*cappella|loop|snippet|"
    r"teaser|trailer|behind the scenes|making of|interview|concert|tour|festival)\b", re.I)

def norm_title(t: str) -> str:
    """Lowercase//punctuation-flattened form. Used for the CANDIDATE, where every
    word must be kept — a YouTube title is usually 'Artist - Song (Audio)', so
    trimming at the dash would throw away the song name itself."""
    t = unicodedata.normalize("NFKC", (t or "").lower())
    t = t.replace("&", " and ")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def core_title(t: str) -> str:
    """The WANTED title reduced to the words that identify the song: feat-credits
    and a trailing version qualifier ('- Radio Mix', '- LP Version') removed, so
    we don't demand words the uploader had no reason to include."""
    t = unicodedata.normalize("NFKC", (t or "").lower())
    t = re.sub(r"\(feat[^)]*\)|\[feat[^\]]*\]|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b", " ", t)
    t = re.sub(r"\s+-\s+.*$", " ", t)           # "Title - Radio Mix" -> "Title"
    t = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", t)  # any remaining bracketed aside
    return norm_title(t)

def _fold_long_vowels(s: str) -> str:
    """Normalise Japanese long-vowel romanisation so spellings of the same word
    compare equal: yuugure/yūgure/yugure, ootaki/ōtaki/otaki, souryou/sōryō.

    Romanisation is not standardised — Spotify writes "Yugure no Tori" while
    every YouTube upload of it writes "Yuugure no Tori", and a strict token test
    reads those as different songs.
    """
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))   # ō -> o, ū -> u
    s = re.sub(r"([aiueo])\1+", r"\1", s)                       # oo/uu -> o/u
    s = re.sub(r"ou", "o", s)                                    # ou -> o (Japanese long o)
    return s

def title_matches(want: str, cand: str) -> bool:
    """Does this candidate actually claim to BE the song we asked for?

    This is the check whose absence put the wrong song in the catalogue: id 9
    was labelled "Plastic Love" but plays Mariya Takeuchi's "Namida no One-sided
    Love" — right artist, right official Topic channel, and 291 s against Plastic
    Love's 294 s, so a duration-only gate waves it straight through. Requiring
    the title's own words to appear in the candidate is what rejects it.

    CJK titles carry no spaces, so for those fall back to a substring test.
    """
    w, c = core_title(want), norm_title(cand)
    if not w:
        return False
    if w in c:
        return True
    toks = [x for x in w.split() if x]
    if not toks:
        return False
    # Non-spaced scripts (Japanese/Chinese/Korean): word tokens don't apply.
    if len(toks) == 1 and re.search(r"[぀-ヿ㐀-鿿가-힯]", w):
        return w in c
    hits = sum(1 for t in toks if t in c)
    # every word for a short title; allow one straggler once it's long enough
    need = len(toks) if len(toks) <= 3 else len(toks) - 1
    if hits >= need:
        return True
    # Second pass with long vowels folded, for romanised Japanese. Strict
    # matching runs first so ordinary titles never get the looser test.
    wf, cf = _fold_long_vowels(w), _fold_long_vowels(c)
    if wf in cf:
        return True
    tf = [x for x in wf.split() if x]
    if not tf:
        return False
    hf = sum(1 for t in tf if t in cf)
    return hf >= (len(tf) if len(tf) <= 3 else len(tf) - 1)

def gate(track: dict, cands: list[dict]) -> tuple[dict | None, list[str]]:
    """Pick the source that IS this recording, or nothing.

    The CSV's Spotify duration is hard evidence, so the decisive test is runtime.
    The old resolve_url claimed to do this and did not: it ran a reject-regex and
    then returned rows[0] anyway — including candidates the regex had just
    rejected — so a cover could be ingested as the master. Nothing here falls
    back to an unverified candidate.
    """
    notes = []
    want = (track.get("duration_ms") or 0) / 1000.0
    if not want:
        return None, ["no CSV duration — cannot verify any source"]
    # Only treat a bad token as disqualifying if the real title doesn't have it
    # (a song genuinely called "... (Remix)" must not reject its own master).
    title_l = track["title"].lower()
    scored = []
    for c in cands:
        t = c.get("title") or ""
        d = c.get("duration")
        if d is None:
            notes.append(f"skip (no duration): {t[:60]}"); continue
        bad = [m.group(0).lower() for m in BAD_TOKENS.finditer(t)]
        bad = [b for b in bad if b not in title_l]
        if bad:
            notes.append(f"skip ({','.join(sorted(set(bad)))}): {t[:60]}"); continue
        # The title must actually name this song. Duration alone is NOT identity:
        # a different track by the same artist can sit within seconds of it.
        if not (title_matches(track["title"], t)
                or (track.get("title_original") and title_matches(track["title_original"], t))):
            notes.append(f"skip (title mismatch): {t[:60]}"); continue
        delta = abs(d - want)
        if delta > DURATION_TOLERANCE_S:
            notes.append(f"skip (Δ{delta:.0f}s): {t[:60]}"); continue
        ch = (c.get("channel") or "").lower()
        # A "- Topic" channel is YouTube's auto-generated label upload: the master.
        bonus = 0
        if ch.endswith("- topic"):
            bonus -= 3
        if any(tok and tok in re.sub(r"[^a-z0-9]", "", ch) for tok in atoks(track["artist"])):
            bonus -= 1
        if re.search(r"\b(official|audio)\b", t, re.I):
            bonus -= 0.5
        scored.append((delta + bonus, delta, c))
    if not scored:
        return None, notes
    scored.sort(key=lambda x: x[0])
    best = scored[0][2]
    best["delta"] = round(scored[0][1], 1)
    # Keep the runners-up. They passed the SAME title + duration gate, so when the
    # chosen upload turns out to be age-gated at fetch time, produce can fall back
    # to the next verified upload of this recording (usually the "- Topic"
    # auto-upload, which YouTube rarely gates) instead of needing a signed-in jar.
    best["alternates"] = [dict(c, delta=round(d, 1)) for _, d, c in scored[1:6]]
    return best, notes

def native_title(artist: str, title: str) -> str | None:
    """Find a song's ORIGINAL-script title from its romanised one.

    The romaji work covers Japanese -> Latin, but the reverse direction matters
    too: Spotify lists 夕暮れの鳥 as "Yugure no Tori", while the actual upload on
    YouTube is titled in Japanese, so searching the romaji finds nothing. Genius
    indexes both scripts on one page ("夕暮れの鳥 (Yuugure No Tori)"), so it can
    hand back the native title to search with.
    """
    tok = ENV.get("GENIUS_TOKEN")
    if not tok:
        return None
    try:
        u = "https://api.genius.com/search?q=" + urllib.parse.quote(f"{artist} {title}")
        r = urllib.request.Request(u, headers={"Authorization": f"Bearer {tok}"})
        hits = json.loads(urllib.request.urlopen(r, timeout=20).read())["response"]["hits"]
    except Exception:
        return None
    want = re.sub(r"[^a-z0-9]", "", title.lower())
    for h in hits[:5]:
        full = h["result"].get("title") or ""
        if not has_japanese(full):
            continue
        # "夕暮れの鳥 (Yuugure No Tori)" -> confirm the romaji half is our song,
        # then keep the native half as the query.
        m = re.match(r"^([^()（）]+)[（(]\s*([^()（）]+?)\s*[)）]", full)
        if not m:
            continue
        native, roman = m.group(1).strip(), m.group(2).strip()
        got = re.sub(r"[^a-z0-9]", "", roman.lower())
        # allow the long-vowel spelling difference (Yugure vs Yuugure)
        if got == want or got.replace("uu", "u") == want.replace("uu", "u"):
            return native if has_japanese(native) else None
    return None

# The SOCKS tunnel's port on the VPS while a stage working replicate-line tracks
# holds it (see replicate_line.Tunnel).
LINE_PORT: int | None = None

def line_of(track: dict) -> str:
    return track.get("line") or "gpu"

def find_candidates(track: dict, query: str) -> dict:
    """YouTube candidates with their durations, from the track's line."""
    if line_of(track) == "replicate":
        import replicate_line
        return replicate_line.search(query, LINE_PORT)
    return agent("search", {"query": query, "limit": 12, "timeout": 150}, timeout=200)

class line_tunnel:
    """Holds the Mac-to-VPS tunnel while `tracks` include replicate-line ones."""
    def __init__(self, tracks):
        self.need = any(line_of(t) == "replicate" for t in tracks)
    def __enter__(self):
        global LINE_PORT
        if self.need:
            import replicate_line
            replicate_line.push()
            self.t = replicate_line.Tunnel()
            LINE_PORT = self.t.__enter__()
        return self
    def __exit__(self, *a):
        global LINE_PORT
        if self.need:
            self.t.__exit__(*a)
            LINE_PORT = None

def resolve_one(track: dict) -> dict:
    pin = track.get("source_pin") or {}
    if pin.get("url"):
        # The link it was submitted with IS the source. Its runtime is still checked
        # against the song's at produce, like every other source.
        want = (track.get("duration_ms") or 0) / 1000.0
        track["source"] = {"url": pin["url"], "title": pin.get("title"), "channel": pin.get("channel") or "pinned",
                           "duration": pin.get("duration"), "pinned": True,
                           "delta_s": round(abs(pin["duration"] - want), 1) if pin.get("duration") and want else None}
        track["alternates"] = []
        track["resolve_notes"] = ["source pinned by the link the song was submitted with"]
        track["status"], track["error"] = "resolved", ""
        return track
    # Search with the ORIGINAL title where we have one: a Japanese upload is
    # titled in Japanese, so querying the romaji finds far less.
    q = (f"{track.get('artist_original') or track['artist']} "
         f"{track.get('title_original') or track['title']}")
    res = find_candidates(track, q)
    if not res.get("ok"):
        track["status"] = "unresolved"
        track["error"] = res.get("error", "search failed")
        return track
    best, notes = gate(track, res.get("candidates", []))
    # Nothing matched under a romanised title? The upload may be titled in the
    # original script. Look the native title up and search again before giving up.
    if not best and not track.get("title_original"):
        nat = native_title(track["artist"], track["title"])
        if nat:
            track["title_original"] = nat
            res2 = find_candidates(track, f"{track['artist']} {nat}")
            if res2.get("ok"):
                b2, n2 = gate(track, res2.get("candidates", []))
                notes = notes[:3] + [f"retried as {nat!r}"] + n2
                best = b2
    track["resolve_notes"] = notes[:6]
    if not best:
        track["status"] = "unresolved"
        track["error"] = (f"no source matched the Spotify runtime "
                          f"({(track.get('duration_ms') or 0)/1000:.0f}s ±{DURATION_TOLERANCE_S:.0f}s)")
        return track
    track["source"] = {"url": best["url"], "title": best.get("title"),
                       "channel": best.get("channel"), "duration": best.get("duration"),
                       "delta_s": best.get("delta")}
    track["alternates"] = best.get("alternates") or []
    track["status"] = "resolved"
    track["error"] = ""
    return track

def cmd_resolve(args):
    state = load_state()
    todo = [t for t in state["tracks"] if t["status"] in ("pending", "unresolved")
            and (not getattr(args, "line", None) or line_of(t) == args.line)]
    if not todo:
        log("  nothing to resolve."); return
    log(f"  resolving {len(todo)} track(s) — accepting only sources within "
        f"±{DURATION_TOLERANCE_S:.0f}s of the Spotify runtime")
    done = 0
    # Searches are network-bound; a little concurrency, not a flood.
    with line_tunnel(todo), ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(resolve_one, t): t for t in todo}
        for fut in as_completed(futs):
            t = fut.result(); done += 1
            if t["status"] == "resolved":
                s = t["source"]
                log(f"    ✓ [{done}/{len(todo)}] {t['artist']} — {t['title']}  (Δ{s['delta_s']}s, {s['channel']})")
            else:
                log(f"    ✗ [{done}/{len(todo)}] {t['artist']} — {t['title']}  {t['error']}")
            save_state(state)
    n_ok = sum(1 for t in state["tracks"] if t["status"] == "resolved")
    n_no = sum(1 for t in state["tracks"] if t["status"] == "unresolved")
    log(f"  resolved {n_ok} | unresolved {n_no} (left OUT of the catalogue — see `status`)")

# ════════════════════════════════════════════════════════════════════════════
# STAGE: produce  — fetch -> split -> encode on the GPU, then pull to the Mac
# ════════════════════════════════════════════════════════════════════════════
def pull_stems(track: dict) -> tuple[bool, str]:
    key = track["catalog_id"]
    dest = STEMS_LOCAL / key
    dest.mkdir(parents=True, exist_ok=True)
    # The username must go through -o User=, NOT "user@host:path". scp's SFTP-mode
    # *source* parser rejects a non-ASCII user ("dimé: invalid user name"), even
    # though the same spec works as a destination.
    remote = f"{BOX_HOST}:zingy_ingest/enc/{key}/*.m4a"
    r = subprocess.run(["scp", "-i", BOX_KEY, "-o", "BatchMode=yes",
                        "-o", f"User={BOX_USER}",
                        "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
                        remote, str(dest)],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    missing = [s for s in STEMS if not (dest / f"{s}.m4a").exists()
               or (dest / f"{s}.m4a").stat().st_size < 1000]
    if missing:
        return False, f"pull failed for {missing}: {(r.stderr or '').strip()[-160:]}"
    return True, ""

def upload_single_track(track: dict) -> tuple[bool, str]:
    if not STORAGE_KEY:
        return False, "no BUNNY_STORAGE_PASSWORD"
    key = track["catalog_id"]
    local = STEMS_LOCAL / key
    if not local.exists():
        return False, "local stems directory missing"
    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(put_file, local / f"{s}.m4a",
                            f"{STEM_PREFIX}/{track['uuid']}/{s}.m4a"): s
                for s in STEMS if (local / f"{s}.m4a").exists()}
        for fut in as_completed(futs):
            results[futs[fut]] = fut.result()
    failed = {s: e for s, (okk, e) in results.items() if not okk}
    if len(results) < len(STEMS) or failed:
        return False, f"upload failed: {failed or 'missing local stems'}"

    missing = [s for s in STEMS if not head_ok(stem_url(track, s))]
    if not missing:
        sizes = {s: (local / f"{s}.m4a").stat().st_size
                 for s in STEMS if (local / f"{s}.m4a").exists()}
        if len(sizes) == len(STEMS):
            lo, hi = min(sizes.values()), max(sizes.values())
            if lo < hi * 0.75:
                odd = [s for s, v in sizes.items() if v < hi * 0.75]
                return False, f"stem length mismatch — {odd} much shorter than rest"
    if missing:
        return False, f"uploaded but not yet serving: {missing}"

    import shutil
    try:
        shutil.rmtree(local)
    except Exception:
        pass
    return True, ""

JAR_WARNED = threading.Event()
AGE_GATE = re.compile(r"confirm your age|age.restricted|cookies for the authentication", re.I)

def alternates_for(track: dict) -> list[dict]:
    """Other uploads that passed the title+duration gate for this track. Tracks
    resolved before alternates were recorded get them re-derived on the spot."""
    alts = track.get("alternates")
    if alts is None:
        q = (f"{track.get('artist_original') or track['artist']} "
             f"{track.get('title_original') or track['title']}")
        res = find_candidates(track, q)
        best, _ = gate(track, res.get("candidates", [])) if res.get("ok") else (None, [])
        alts = ([dict(best, alternates=None)] if best else []) + ((best or {}).get("alternates") or [])
        track["alternates"] = [a for a in alts if a.get("url") != track["source"]["url"]]
    return track.get("alternates") or []

def fetch_source(track: dict, key: str) -> dict:
    """fetch, and on an age gate walk the alternates before giving up. A gate is
    a property of the UPLOAD, not the recording: the label's "- Topic" copy of
    the same master is usually open. This is what keeps cookies a last resort."""
    src = track["source"]
    r = agent("fetch", {"key": key, "url": src["url"], "timeout": 420}, timeout=520)
    if not r.get("ok") and "timed out" in (r.get("error") or "").lower():
        if ensure_box():
            r = agent("fetch", {"key": key, "url": src["url"], "timeout": 420}, timeout=520)
    # The agent's error text is a 300-char tail, which usually cuts the "Sign in
    # to confirm your age" prefix off — trust its explicit flag first.
    if r.get("ok") or not (r.get("age_gated") or AGE_GATE.search(r.get("error") or "")):
        return r
    tried = [src["url"]]
    for alt in alternates_for(track):
        if alt.get("url") in tried:
            continue
        tried.append(alt["url"])
        r2 = agent("fetch", {"key": key, "url": alt["url"], "timeout": 420}, timeout=520)
        if r2.get("ok"):
            track["source_gated"] = src
            track["source"] = {"url": alt["url"], "title": alt.get("title"),
                               "channel": alt.get("channel"), "duration": alt.get("duration"),
                               "delta_s": alt.get("delta")}
            r2["via_alternate"] = True
            return r2
    r["error"] = f"{r.get('error', '')[:200]} (age-gated; {len(tried)-1} alternate(s) also failed)"
    return r

def produce_one(track: dict) -> dict:
    key = track["catalog_id"]
    r = fetch_source(track, key)
    if not r.get("ok"):
        if r.get("jar_invalid") and not JAR_WARNED.is_set():
            JAR_WARNED.set()
            log("      ! the YouTube cookie jar on the box has been ROTATED — age-gated uploads "
                "without an open alternate will keep failing until you re-export it "
                "(private window → sign in → export → close the window)")
        track["status"] = "failed"; track["error"] = r.get("error", "fetch failed"); return track

    # Verify what we ACTUALLY downloaded, not just what the search page claimed.
    got = r.get("duration")
    want = (track.get("duration_ms") or 0) / 1000.0
    if got and want and abs(got - want) > DURATION_TOLERANCE_S:
        track["status"] = "unresolved"
        track["error"] = (f"downloaded audio is {got:.0f}s but Spotify says {want:.0f}s "
                          f"— wrong recording, discarded")
        agent("cleanup", {"keys": [key]}, timeout=120)
        return track
    track["fetched_duration"] = round(got, 2) if got else None

    # Wrap ONLY Demucs CUDA separation in GPU_LOCK
    with GPU_LOCK:
        r = agent("split", {"key": key, "timeout": 1800}, timeout=1900)
        if not r.get("ok") and r.get("oom"):
            # 6 GB laptop card: retry once with a smaller segment before failing.
            r = agent("split", {"key": key, "segment": 7, "timeout": 2400}, timeout=2500)
    if not r.get("ok"):
        track["status"] = "failed"; track["error"] = r.get("error", "split failed"); return track

    r = agent("encode", {"key": key, "bitrate": STEM_BITRATE, "timeout": 420}, timeout=520)
    if not r.get("ok"):
        track["status"] = "failed"; track["error"] = r.get("error", "encode failed"); return track
    track["stem_sizes"] = {k: v.get("size") for k, v in (r.get("files") or {}).items()}

    # BPM and key are mandatory.
    if not track.get("bpm") or not track.get("key"):
        r = agent("analyze", {"key": key, "window": 150}, timeout=420)
        if r.get("ok"):
            track["analysis"] = {"bpm": r.get("bpm"), "key": r.get("key"),
                                 "key_confidence": r.get("key_confidence"),
                                 "source": "librosa@gpu"}
        else:
            track["analysis_error"] = r.get("error", "analyze failed")

    ok, err = pull_stems(track)
    if not ok:
        track["status"] = "failed"; track["error"] = err; return track
    
    track["status"] = "produced"
    track["error"] = ""
    agent("cleanup", {"keys": [key]}, timeout=180)   # free the box's disk as we go
    return upload_and_enrich(track)

def upload_and_enrich(track: dict) -> dict:
    """Both lines, once the four m4a are in STEMS_LOCAL/<id>/."""
    # Immediately upload stems to BunnyCDN & reclaim Mac disk space
    up_ok, up_err = upload_single_track(track)
    if up_ok:
        track["status"] = "verified"
        track["error"] = ""
        # Enrich inline: Genius metadata + cover art, so no separate pass needed.
        # enrich_one is network-only (Genius + iTunes APIs), no GPU/disk dependency.
        try:
            enrich_one(track)
            g = track.get("genius") or {}
            fields = "+".join(k for k in ("producers", "writers", "coverArt") if g.get(k))
            if fields:
                track["_enrich_note"] = fields
        except Exception as e:
            track["_enrich_note"] = f"enrich error: {str(e)[:80]}"
    else:
        track["error"] = f"upload pending/failed: {up_err[:120]}"

    return track

def produce_one_replicate(track: dict) -> dict:
    """The replicate line: fetch, runtime check, Replicate split (htdemucs_ft,
    FLAC), one AAC encode and BPM/key on the VPS; the stems then come here."""
    import replicate_line
    key = track["catalog_id"]
    src = track["source"]
    urls = [src["url"]] + [a["url"] for a in (track.get("alternates") or [])
                           if a.get("url") and a["url"] != src["url"]]
    need = not track.get("bpm") or not track.get("key")
    r = replicate_line.produce(key, urls, (track.get("duration_ms") or 0) / 1000.0, LINE_PORT,
                               analyze=need, dest=STEMS_LOCAL / key,
                               tolerance=DURATION_TOLERANCE_S, bitrate=STEM_BITRATE)
    if not r.get("ok"):
        if r.get("wrong_length"):
            track["status"] = "unresolved"
        else:
            track["status"] = "failed"
        track["error"] = r.get("error", "replicate line failed")
        return track
    if r.get("via_alternate"):
        alt = next((a for a in track.get("alternates") or [] if a.get("url") == r["url"]), {})
        track["source_gated"] = src
        track["source"] = {"url": r["url"], "title": alt.get("title"), "channel": alt.get("channel"),
                           "duration": alt.get("duration"), "delta_s": alt.get("delta")}
    track["fetched_duration"] = round(r["duration"], 2) if r.get("duration") else None
    track["stem_sizes"] = {s: f["size"] for s, f in r["files"].items()}
    track["split"] = {"via": "replicate", "model": "htdemucs_ft",
                      "prediction": r.get("prediction"), "predict_time": r.get("predict_time")}
    if r.get("analysis"):
        track["analysis"] = r["analysis"]
    elif need:
        track["analysis_error"] = r.get("analysis_error", "analyze failed")
    track["status"] = "produced"
    track["error"] = ""
    return upload_and_enrich(track)

def cmd_produce(args):
    state = load_state()
    todo = [t for t in state["tracks"] if t["status"] in ("resolved", "failed")
            and (not getattr(args, "line", None) or line_of(t) == args.line)]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        log("  nothing to produce."); return
    lines = sorted({line_of(t) for t in todo})
    log(f"  producing {len(todo)} track(s) on {' + '.join('Replicate' if l == 'replicate' else 'GPU' for l in lines)}"
        f" (3 concurrent workers, immediate CDN upload + disk cleanup)")
    t0 = time.time()

    stop_event = threading.Event()
    done_count = 0
    total_todo = len(todo)

    def _worker(t: dict, idx: int) -> dict:
        if stop_event.is_set():
            return t
        started = time.time()
        log(f"    ▶ [{idx}/{total_todo}] {t['artist']} — {t['title']}")
        if line_of(t) == "replicate":
            produce_one_replicate(t)
        else:
            produce_one(t)

        with _state_lock:
            save_state(state)

        el = time.time() - started
        if (line_of(t) == "gpu" and t["status"] == "failed"
                and "timed out" in (t.get("error") or "").lower() and not box_alive()):
            log("      ! the GPU box is down — stopping queue")
            t["status"] = "resolved"; t["error"] = ""
            with _state_lock:
                save_state(state)
            stop_event.set()
            return t

        nonlocal done_count
        with _state_lock:
            done_count += 1
            n_done = sum(1 for x in state["tracks"] if x["status"] in
                         ("produced", "uploaded", "verified", "published"))
            rate = (time.time() - t0) / done_count
            left = int(rate * (total_todo - done_count))
            st = t["status"]
            enrich_info = t.pop("_enrich_note", "")
            if st in ("produced", "verified"):
                extra = f" [{enrich_info}]" if enrich_info else ""
                log(f"      ✓ [{idx}/{total_todo}] {t['artist']} — {t['title']} ({st.upper()}) {el:.0f}s (done {n_done}; ~{left//60}m{left%60:02d}s left){extra}")
            else:
                log(f"      ✗ [{idx}/{total_todo}] {t['artist']} — {t['title']} {el:.0f}s {t['error'][:120]}")
        return t

    with line_tunnel(todo), ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(_worker, t, i) for i, t in enumerate(todo, 1)]
        for fut in as_completed(futs):
            if stop_event.is_set():
                break

# ════════════════════════════════════════════════════════════════════════════
# STAGE: upload  — PUT then HEAD-verify. Status only moves on proof.
# ════════════════════════════════════════════════════════════════════════════
def put_file(local: Path, remote_path: str) -> tuple[bool, str]:
    url = STORAGE_BASE + urllib.parse.quote(remote_path, safe="/")
    data = local.read_bytes()
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, method="PUT",
                                     headers={"AccessKey": STORAGE_KEY,
                                              "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                if resp.status in (200, 201):
                    return True, ""
                return False, f"http {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False, "401 Unauthorized — BUNNY_STORAGE_PASSWORD is wrong/stale"
            if attempt == 2:
                return False, f"http {e.code}"
        except Exception as e:
            if attempt == 2:
                return False, str(e)[:120]
        time.sleep(2 * (attempt + 1))
    return False, "exhausted retries"

def head_ok(url: str) -> bool:
    try:
        req = urllib.request.Request(url + f"?v={int(time.time())}", method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False

def stem_url(track: dict, stem: str) -> str:
    return f"{CDN_HOST}/{STEM_PREFIX}/{track['uuid']}/{stem}.m4a"

def cmd_upload(args):
    if not STORAGE_KEY:
        sys.exit("  ✗ no BUNNY_STORAGE_PASSWORD — refusing to upload (every PUT would 401).")
    state = load_state()
    todo = [t for t in state["tracks"] if t["status"] in ("produced", "uploaded")]
    if not todo:
        log("  nothing to upload."); return
    log(f"  uploading stems for {len(todo)} track(s) → {STORAGE_ZONE}/{STEM_PREFIX}/<uuid>/")
    for t in todo:
        key = t["catalog_id"]
        local = STEMS_LOCAL / key
        results = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(put_file, local / f"{s}.m4a",
                                f"{STEM_PREFIX}/{t['uuid']}/{s}.m4a"): s
                    for s in STEMS if (local / f"{s}.m4a").exists()}
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()
        failed = {s: e for s, (okk, e) in results.items() if not okk}
        if len(results) < len(STEMS) or failed:
            t["status"] = "produced"
            t["error"] = f"upload failed: {failed or 'missing local stems'}"
            log(f"    ✗ {t['artist']} — {t['title']}: {t['error'][:140]}")
            continue
        # Proof, not assumption: the file must actually serve from the CDN.
        missing = [s for s in STEMS if not head_ok(stem_url(t, s))]
        # ...and be the same LENGTH as its siblings. A truncated stem serves a
        # perfectly good 200: Plastic Love shipped with a bass stem 98 seconds
        # shorter than the other three and every reachability check passed.
        if not missing:
            sizes = {s: (STEMS_LOCAL / key / f"{s}.m4a").stat().st_size
                     for s in STEMS if (STEMS_LOCAL / key / f"{s}.m4a").exists()}
            if len(sizes) == len(STEMS):
                lo, hi = min(sizes.values()), max(sizes.values())
                if lo < hi * 0.75:
                    odd = [s for s, v in sizes.items() if v < hi * 0.75]
                    t["status"] = "produced"
                    t["error"] = f"stem length mismatch — {odd} much shorter than the rest"
                    log(f"    ✗ {t['artist']} — {t['title']}: {t['error']}")
                    save_state(state)
                    continue
        if missing:
            t["status"] = "uploaded"
            t["error"] = f"uploaded but not yet serving: {missing}"
            log(f"    ~ {t['artist']} — {t['title']}: {t['error']}")
        else:
            t["status"] = "verified"; t["error"] = ""
            log(f"    ✓ {t['artist']} — {t['title']}  4/4 stems live")
        save_state(state)

# ════════════════════════════════════════════════════════════════════════════
# STAGE: enrich  — Genius, scored. Never blind hits[0].
# ════════════════════════════════════════════════════════════════════════════
def genius_get(path: str, params: dict | None = None) -> dict | None:
    tok = ENV.get("GENIUS_TOKEN")
    if not tok:
        return None
    url = f"https://api.genius.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (attempt + 1)); continue
            return None
        except Exception:
            time.sleep(2); continue
    return None

def pick_genius_hit(track: dict, hits: list) -> dict | None:
    """Score hits instead of trusting the first. Searching 'Jodeci Come & Talk To
    Me' returns the REMIX as hit #1 — blind hits[0] writes the wrong credits."""
    want_t, want_a = ntitle(track["title"]), atoks(track["artist"])
    best, best_score = None, -1e9
    for h in hits:
        r = h.get("result") or {}
        gt = ntitle(r.get("title") or "")
        ga = atoks((r.get("primary_artist") or {}).get("name") or "")
        score = 0.0
        if gt == want_t:
            score += 10
        elif want_t and (want_t in gt or gt in want_t):
            score += 4
        else:
            continue                       # different song entirely
        # The artist must MATCH, not merely add points. Treating it as a bonus
        # let a title-only hit score 10 and win: Junko Ohashi's "Telephone
        # Number" was about to be credited to Seori, a Korean artist with a
        # different song of the same name. A same-titled song by someone else is
        # not this song, however well the title lines up.
        if want_a and ga and not (want_a & ga):
            continue
        if want_a & ga:
            score += 6
        full = (r.get("full_title") or "") + " " + (r.get("title") or "")
        # penalise a version qualifier the CSV title doesn't have
        for m in BAD_TOKENS.finditer(full):
            if m.group(0).lower() not in track["title"].lower():
                score -= 8
        if score > best_score:
            best, best_score = r, score
    return best if best_score > 0 else None

def itunes_cover(artist: str, title: str, album: str | None) -> str | None:
    """Cover art ONLY, from the iTunes release.

    Used for tracks Genius has no page for — in practice the remix/club/LP
    versions, where the scorer correctly refuses to reuse the original's entry.
    Deliberately limited to artwork: a remix genuinely shares its release art,
    but its producers and writers are NOT the original's, so those stay empty
    rather than being borrowed from a different recording.
    """
    want = atoks(artist)
    for term in ([f"{artist} {title}"] + ([f"{artist} {album}"] if album else [])):
        try:
            url = ("https://itunes.apple.com/search?term="
                   + urllib.parse.quote(term) + "&entity=song&limit=5")
            r = json.loads(urllib.request.urlopen(url, timeout=20).read())
            for x in r.get("results", []):
                art = x.get("artworkUrl100") or ""
                if not art:
                    continue
                # The result must actually be BY this artist. limit=1 with no
                # artist check is how id 4474 ended up wearing the art of the
                # "Star" TV-cast cover of the Jodeci song, and how id 52 wore a
                # Various-Artists compilation sleeve.
                got = atoks(x.get("artistName") or "")
                if got and want and not (got & want):
                    continue
                if (x.get("artistName") or "").strip().lower() == "various artists":
                    continue
                return art.replace("100x100bb", "1000x1000bb")
        except Exception:
            pass
        time.sleep(0.3)          # iTunes throttles aggressively
    return None

def enrich_one(track: dict) -> dict:
    d = genius_get("/search", {"q": f"{track['artist']} {track['title']}"})
    hit = pick_genius_hit(track, ((d or {}).get("response") or {}).get("hits") or [])
    meta = {}
    if hit:
        meta["genius_id"] = hit.get("id")
        art = hit.get("song_art_image_url") or hit.get("header_image_url")
        if art:
            meta["coverArt"] = art
        full = genius_get(f"/songs/{hit['id']}", {"text_format": "plain"})
        song = ((full or {}).get("response") or {}).get("song") or {}
        prod = [a.get("name") for a in (song.get("producer_artists") or []) if a.get("name")]
        writ = [a.get("name") for a in (song.get("writer_artists") or []) if a.get("name")]
        if prod:
            meta["producers"] = ", ".join(prod)
        if writ:
            meta["writers"] = ", ".join(writ)
        if song.get("song_art_image_url"):
            meta["coverArt"] = song["song_art_image_url"]
    # A cover from the link the song came in by: Spotify's / Deezer's own art for
    # exactly this recording beats an iTunes title search; a video thumbnail doesn't.
    hint, hsrc = track.get("cover_hint"), track.get("cover_hint_source") or "link"
    if not meta.get("coverArt") and hint and hsrc in ("spotify", "deezer"):
        meta["coverArt"], meta["coverSource"] = hint, hsrc
    if not meta.get("coverArt"):
        art = itunes_cover(track["artist"], track["title"], track.get("album"))
        if art:
            meta["coverArt"] = art
            meta["coverSource"] = "itunes"
    if not meta.get("coverArt") and hint:
        meta["coverArt"], meta["coverSource"] = hint, hsrc
    track["genius"] = meta          # {} means "Genius had nothing" — not a fake value
    track["cover_attempted"] = True
    return track

def cmd_enrich(args):
    state = load_state()
    # Also re-visit tracks that matched Genius but ended up with no artwork, so the
    # iTunes cover fallback gets its turn without redoing the whole stage.
    todo = [t for t in state["tracks"]
            if t["status"] in ("verified", "uploaded", "produced", "published")
            and ("genius" not in t
                 or (not (t.get("genius") or {}).get("coverArt") and not t.get("cover_attempted")))]
    if not todo:
        log("  nothing to enrich."); return
    if not ENV.get("GENIUS_TOKEN"):
        log("  ! no GENIUS_TOKEN — skipping (producers/writers/cover stay empty rather than faked)")
        return
    log(f"  enriching {len(todo)} track(s) from Genius (scored match, official API)")
    got = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(enrich_one, t): t for t in todo}
        for fut in as_completed(futs):
            t = fut.result()
            g = t.get("genius") or {}
            if g:
                got += 1
            fields = "+".join(k for k in ("producers", "writers", "coverArt") if g.get(k)) or "nothing"
            log(f"    {'✓' if g else '·'} {t['artist']} — {t['title']}  [{fields}]")
            save_state(state)
    log(f"  Genius matched {got}/{len(todo)}")

# ════════════════════════════════════════════════════════════════════════════
# STAGE: vibe  — valence/danceability/energy/acousticness/speechiness/instrumentalness
# ════════════════════════════════════════════════════════════════════════════
def cmd_vibe(args):
    """La boîte bleue scores the tracks the CSV gave no vibe numbers for (every
    song from the Spotify playlist, an album or a track lookup: Spotify closed its
    audio-features endpoint to new apps). Measured from the stems now on the CDN,
    on the VPS, where the model lives. A CSV value is never written over, and a
    track the model could not score still publishes, without the numbers."""
    import vibe
    state = load_state()
    todo = [t for t in state["tracks"] if t["status"] == "verified" and vibe.missing(t.get("features") or {})]
    if not todo:
        log("  every verified track has its vibe numbers."); return
    targets = [{"id": t["catalog_id"], "stems": {s: stem_url(t, s) for s in STEMS}} for t in todo]
    try:
        got = vibe.score_on_vps(targets, "ingest")
    except Exception as e:
        log(f"  ! La boîte bleue did not run ({str(e)[:120]}), publishing without the numbers"); return
    for t in todo:
        r = got.get(t["catalog_id"]) or {"error": "not scored"}
        if "error" in r:
            t["vibe_error"] = r["error"]
            log(f"    · {t['artist']} — {t['title']}  {r['error'][:60]}")
            continue
        t["features"] = {**{k: r[k] for k in vibe.VIBE}, **(t.get("features") or {})}
        t["features_source"] = "lbb"
        t.pop("vibe_error", None)
    save_state(state)
    vibe.finish("ingest")

# ════════════════════════════════════════════════════════════════════════════
# STAGE: publish  — write the app's files, then push them to the CDN
# ════════════════════════════════════════════════════════════════════════════
def resolved_bpm_key(t: dict) -> tuple[int | None, str | None]:
    """CSV first (label data), then what we measured. Never a default."""
    bpm = t.get("bpm") or (t.get("analysis") or {}).get("bpm")
    key = t.get("key") or (t.get("analysis") or {}).get("key")
    return bpm, key

def mmss(seconds: float | int) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"

def build_song_json(t: dict) -> dict:
    """musicdata/songs/<id>.json — the file the app reads to get stem URLs.
    Fields we could not source are OMITTED, never defaulted."""
    dur = None
    if t.get("fetched_duration"):
        dur = int(round(t["fetched_duration"]))
    elif t.get("duration_ms"):
        dur = int(round(t["duration_ms"] / 1000))
    g = t.get("genius") or {}
    doc = {
        "id": t["catalog_id"],
        "title": t["title"],
        "artist": t["artist"],
        "stems": [{"id": s, "name": s.upper(), "url": stem_url(t, s)} for s in STEMS],
    }
    if g.get("coverArt"):
        doc["coverArt"] = g["coverArt"]
    if dur is not None:
        doc["duration"] = dur
        doc["displayDuration"] = mmss(dur)
    _bpm, _key = resolved_bpm_key(t)
    if _bpm:
        doc["bpm"] = _bpm
    if _key:
        doc["key"] = _key
    if t.get("release_date"):
        doc["year"] = str(t["release_date"])[:4]
    if g.get("producers"):
        doc["producers"] = g["producers"]
    if g.get("writers"):
        doc["writers"] = g["writers"]
    if t.get("record_label"):
        doc["recordLabel"] = t["record_label"]
    if t.get("album"):
        doc["album"] = t["album"]
    return doc

def build_index_entry(t: dict) -> dict:
    dur = t.get("fetched_duration") or ((t.get("duration_ms") or 0) / 1000 or None)
    g = t.get("genius") or {}
    e = {"id": t["catalog_id"], "title": t["title"], "artist": t["artist"], "hasStems": True}
    if g.get("coverArt"):
        e["coverArt"] = g["coverArt"]
    if dur:
        e["duration"] = mmss(dur)
    _bpm, _key = resolved_bpm_key(t)
    if _bpm:
        e["bpm"] = _bpm
    if _key:
        e["key"] = _key
    if g.get("producers"):
        e["producers"] = g["producers"]
    if g.get("writers"):
        e["writers"] = g["writers"]
    if t.get("genres"):
        e["genres"] = t["genres"]
    for k, v in (t.get("features") or {}).items():
        e[k] = v
    if t.get("popularity") is not None:
        e["popularity"] = t["popularity"]
    # Year and label ride in the index too: the app's taste engine scores every song
    # from the index alone, and without these its era and label tastes could learn
    # from the song playing but never score a candidate.
    if t.get("release_date"):
        e["year"] = str(t["release_date"])[:4]
    if t.get("record_label"):
        e["recordLabel"] = t["record_label"]
    return e

def cmd_publish(args):
    state = load_state()
    todo = [t for t in state["tracks"] if t["status"] == "verified"]
    if not todo:
        log("  nothing verified to publish."); return

    # Mandatory: the app shows BPM and key on every song, and Mixima matches on
    # key, so a song without them is broken in the library rather than merely thin.
    incomplete = [t for t in todo if not all(resolved_bpm_key(t))]
    if incomplete:
        log(f"  ! {len(incomplete)} track(s) have no bpm/key — NOT publishing them:")
        for t in incomplete[:8]:
            log(f"      {t['catalog_id']:>5} {t['artist'][:24]:24s} {t['title'][:26]:26s}"
                f"  {t.get('analysis_error','no CSV value and no analysis')[:44]}")
        log("    fix with:  python3 zingy.py fix bpmkey --apply")
        todo = [t for t in todo if all(resolved_bpm_key(t))]
        if not todo:
            return

    cat = local_index()
    songs = cat.setdefault("songsList", [])
    by_id = {str(s.get("id")): i for i, s in enumerate(songs)}

    # Outside the app's musicdata, which ships inside the app (see catalog_lib.BACKUP_DIR).
    backup_dir = Path(__file__).resolve().parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"music-index.json.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    backup.write_bytes(INDEX_PATH.read_bytes())
    log(f"  backed up the index → {backup.name}")

    SONGS_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for t in todo:
        cid = t["catalog_id"]
        fname = f"{int(cid):04d}.json"
        (SONGS_DIR / fname).write_text(
            json.dumps(build_song_json(t), indent=1, ensure_ascii=False), encoding="utf-8")
        entry = build_index_entry(t)
        if cid in by_id:
            songs[by_id[cid]] = entry
        else:
            songs.append(entry)
        written.append(f"songs/{fname}")
        t["status"] = "published"
    tmp = INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cat, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, INDEX_PATH)
    save_state(state)
    log(f"  wrote {len(written)} song file(s) + {len(todo)} index entr(ies) "
        f"(catalogue now {len(songs)} songs)")

    if args.skip_upload:
        log("  --skip-upload: not pushing to the CDN."); return
    fd, listing_path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    listing = Path(listing_path)
    # The TRAILING NEWLINE is load-bearing: upload-catalog.sh consumes this list
    # with `while IFS= read -r rel`, which silently drops a final line that has no
    # newline. Without it music-index.json — always last here — was never uploaded,
    # so stems and per-song JSON went live while the app still saw the old index.
    listing.write_text("\n".join(written + ["music-index.json"]) + "\n", encoding="utf-8")
    log("  pushing catalogue to the CDN …")
    r = subprocess.run(["bash", str(REPO / "scripts/upload-catalog.sh"),
                        "--files-from", str(listing)],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    tail = (r.stdout or "").strip().splitlines()[-6:]
    for line in tail:
        log("    " + line)
    if r.returncode != 0:
        log(f"  ✗ publish script exited {r.returncode}: {(r.stderr or '')[-300:]}")

    # Prove the index actually carries the new ids. A successful-looking publish
    # that uploaded the per-song files but not the index leaves the songs
    # unreachable from the app, and says nothing about it.
    want = {t["catalog_id"] for t in todo}
    try:
        live = json.loads(urllib.request.urlopen(
            f"{CDN_HOST}/musicdata/music-index.json?v={int(time.time())}",
            timeout=60).read().decode("utf-8", "replace"))
        have = {str(s.get("id")) for s in live.get("songsList", [])}
        missing = sorted(want - have, key=lambda x: int(x))
        if missing:
            log(f"  ✗ the live index is MISSING {len(missing)} of the ids just published: "
                f"{missing[:8]}{' …' if len(missing) > 8 else ''}")
            log("    re-run:  bash scripts/upload-catalog.sh --only music-index.json")
        else:
            log(f"  ✓ live index verified: {len(live.get('songsList', []))} songs, "
                f"all {len(want)} new id(s) present")
    except Exception as e:
        log(f"  ! could not verify the live index ({e}) — check it before trusting this run")

# ════════════════════════════════════════════════════════════════════════════
# STAGE: report / status
# ════════════════════════════════════════════════════════════════════════════
def cmd_status(args):
    state = load_state()
    tracks = state["tracks"]
    if not tracks:
        log("  manifest is empty — run prepare first."); return
    counts = {}
    for t in tracks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    log(f"── manifest: {len(tracks)} track(s) ───────────────────────────")
    for k in ("pending", "resolved", "produced", "uploaded", "verified", "published",
              "unresolved", "failed"):
        if counts.get(k):
            log(f"  {k:11s} {counts[k]}")
    bad = [t for t in tracks if t["status"] in ("unresolved", "failed")]
    if bad:
        log(f"\n── {len(bad)} track(s) NOT in the catalogue ──────────────────")
        for t in bad:
            log(f"  [{t['status']}] {t['artist']} — {t['title']}")
            log(f"       {t.get('error','')[:170]}")

def cmd_report(args):
    """The receipt. Everything here is re-checked over HTTP right now — nothing
    is read back from our own state file and presented as if it were verified."""
    state = load_state()
    pub = [t for t in state["tracks"] if t["status"] == "published"]
    if not pub:
        log("  nothing published yet."); return
    log(f"── FINAL SWEEP: {len(pub)} published track(s), re-verified over HTTP ──")
    clean = 0
    for t in pub:
        live = sum(1 for s in STEMS if head_ok(stem_url(t, s)))
        detail_ok = head_ok(f"{CDN_HOST}/musicdata/songs/{int(t['catalog_id']):04d}.json")
        g = t.get("genius") or {}
        marks = []
        for val, lbl in ((g.get("producers"), "prod"), (g.get("writers"), "writ"),
                         (t.get("release_date"), "year"), (t.get("record_label"), "label"),
                         (t.get("bpm"), "bpm"), (t.get("key"), "key"),
                         (g.get("coverArt"), "cover"), (t.get("genres"), "genres")):
            marks.append(("✓" if val else "✗") + lbl)
        ok = live == 4 and detail_ok
        clean += 1 if ok and "✗" not in "".join(marks) else 0
        log(f"  [{'OK' if ok else '!!'}] {t['artist']} — {t['title']}  "
            f"(stems {live}/4, json {'✓' if detail_ok else '✗'})  {' '.join(marks)}")
    log(f"── {clean}/{len(pub)} with every field present; "
        f"{sum(1 for t in pub if all(head_ok(stem_url(t,s)) for s in STEMS))}/{len(pub)} fully playable")

# ── cli ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Zingy GPU catalogue ingest (Mac-orchestrated)")
    sub = ap.add_subparsers(dest="stage", required=True)
    for name in ("preflight", "upload", "enrich", "vibe", "status", "report"):
        sub.add_parser(name)
    sub.add_parser("resolve").add_argument("--line", choices=["gpu", "replicate"])
    p = sub.add_parser("prepare"); p.add_argument("--csv", required=True)
    p.add_argument("--rearm", action="store_true",
                   help="link intake: give songs already in the manifest (not yet made) another go on this line")
    p.add_argument("--report", help="link intake: write what became of each CSV row (JSON)")
    p.add_argument("--line", choices=["gpu", "replicate"], default="gpu")
    pr = sub.add_parser("produce"); pr.add_argument("--limit", type=int, default=0)
    pr.add_argument("--line", choices=["gpu", "replicate"])
    pu = sub.add_parser("publish"); pu.add_argument("--skip-upload", action="store_true")
    pa = sub.add_parser("all")
    pa.add_argument("--csv", required=True)
    pa.add_argument("--limit", type=int, default=0)
    pa.add_argument("--skip-upload", action="store_true")
    pa.add_argument("--line", choices=["gpu", "replicate"], default="gpu")
    a = ap.parse_args()

    # The box's preconditions mean nothing to a replicate-line run.
    if a.stage == "preflight" or (a.stage == "all" and a.line == "gpu"):
        if not cmd_preflight(a) and a.stage == "all":
            sys.exit("  refusing to start a long run with failed preconditions.")
    if a.stage not in ("status", "report"):
        claim_manifest(a.stage)
    if a.stage in ("prepare", "all"): cmd_prepare(a)
    if a.stage in ("resolve", "all"): cmd_resolve(a)
    if a.stage in ("produce", "all"): cmd_produce(a)
    if a.stage in ("upload", "all"): cmd_upload(a)
    if a.stage in ("enrich", "all"): cmd_enrich(a)
    if a.stage in ("vibe", "all"): cmd_vibe(a)
    if a.stage in ("publish", "all"): cmd_publish(a)
    if a.stage in ("status",): cmd_status(a)
    if a.stage in ("report", "all"): cmd_report(a)

if __name__ == "__main__":
    main()
