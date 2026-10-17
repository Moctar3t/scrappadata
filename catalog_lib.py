#!/usr/bin/env python3
"""
catalog_lib.py — the ONE place that reads, edits, publishes and verifies the
catalogue. Every repair goes through here.

WHY THIS EXISTS
  Fixing "Plastic Love" and the "presidential" mislabel were done twice, in two
  different ad-hoc ways — one as a throwaway script, one as inline Python typed
  at the shell. Both re-implemented the same four steps (load index, edit the
  index entry, edit the per-song JSON, publish and verify) and both could have
  got any of them subtly wrong. A catalogue edit is not a place for improvisation:
  it writes the file the app reads.

WHAT IT GUARANTEES
  · the index is backed up before every write, and written atomically
  · the per-song JSON is fetched from the CDN when there is no local copy
    (legacy songs have no local file; editing only the index would leave the
    detail file contradicting it)
  · the publish list ALWAYS ends with a newline — upload-catalog.sh reads it
    with `while read`, which silently drops a final unterminated line, and that
    line is music-index.json
  · after publishing it re-fetches the live index and PROVES the change landed
"""
from __future__ import annotations
import json, os, re, subprocess, tempfile, time, urllib.request
from pathlib import Path

REPO = Path(os.environ.get("ZINGY_REPO", "/Users/dime/Developer/ios-zingy"))
MUSICDATA = REPO / "App/ZingyApp/Resources/musicdata"
INDEX_PATH = MUSICDATA / "music-index.json"
SONGS_DIR = MUSICDATA / "songs"
# Index backups: outside the app repo's musicdata, which ships inside the app.
BACKUP_DIR = Path(__file__).resolve().parent / "backups"
CDN = "https://dimesystemsstorage.b-cdn.net"
STEMS = ["bass", "drums", "other", "vocals"]

def log(m): print(m, flush=True)

def song_filename(cid) -> str:
    """The app resolves detail files as %04d for numeric ids (Catalog.swift)."""
    cid = str(cid)
    return f"{int(cid):04d}.json" if cid.isdigit() else f"{cid}.json"

# Snapshot of the index as each caller loaded it, so a save can tell whether the
# file changed underneath. Keyed by the identity of the dict handed out.
_loaded_digest: dict[int, str] = {}

def _index_digest() -> str:
    import hashlib
    return hashlib.sha256(INDEX_PATH.read_bytes()).hexdigest()

# ── the LIVE index is the truth ─────────────────────────────────────────────
# Two machines write the catalogue: the VPS publishes ingests around the clock,
# and this Mac's tools (fix, relabel, dedupe, …) edit it. Each used to write its
# own local copy over the whole live index, so whichever wrote second erased what
# the other had added. Now every load starts from the live index, and every save
# is a three-way merge by song id: this command's own edits applied on top of
# whatever is live at that moment.
_loaded_base: dict[int, dict] = {}

def _env() -> dict:
    env = {}
    for p in (REPO / "scripts/.env",):
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env

def live_index() -> dict:
    """The index as Bunny storage holds it (authoritative: no edge cache)."""
    e = _env()
    if e.get("BUNNY_STORAGE_PASSWORD") and e.get("BUNNY_STORAGE_ZONE"):
        host = e.get("BUNNY_STORAGE_HOST") or "storage.bunnycdn.com"
        req = urllib.request.Request(f"https://{host}/{e['BUNNY_STORAGE_ZONE']}/musicdata/music-index.json",
                                     headers={"AccessKey": e["BUNNY_STORAGE_PASSWORD"]})
    else:
        req = urllib.request.Request(f"{CDN}/musicdata/music-index.json?v={int(time.time())}")
    with urllib.request.urlopen(req, timeout=60) as r:
        cat = json.loads(r.read().decode("utf-8"))
    if not isinstance(cat.get("songsList"), list) or len(cat["songsList"]) < 1000:
        raise RuntimeError(f"live index looks wrong ({len(cat.get('songsList') or [])} songs)")
    return cat

# What the live index was at the last sync. The base of every merge: anything the
# local copy changed relative to it is a local edit not yet published, and must
# survive; anything live changed relative to it came from the other machine.
# Kept beside the backups, never in musicdata (the app bundles that folder).
LIVE_SNAPSHOT = BACKUP_DIR / "live-index.json"

def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _write_index(cat: dict) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cat, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, INDEX_PATH)

def mark_published() -> None:
    """After a verified publish, live == local: that is the new merge base."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_SNAPSHOT.write_bytes(INDEX_PATH.read_bytes())

def sync_from_live(required: bool = False) -> bool:
    """Three-way: this copy's unpublished edits (vs the last live snapshot), applied
    onto the live index as it is now. Neither side loses anything."""
    try:
        live = live_index()
    except Exception as e:
        if required:
            raise RuntimeError(f"could not read the live index ({e}) — refusing to write over it blind")
        log(f"    ! live index unreachable ({e}); working from the local copy")
        return False
    local, snap = _read_json(INDEX_PATH), _read_json(LIVE_SNAPSHOT)
    if local is None:
        result = live
    elif snap is None:
        # No record of what was live: local edits can't be told from staleness.
        # Take live (the safe side: nothing removed elsewhere comes back).
        extra = len({str(x.get("id")) for x in local.get("songsList", [])}
                    - {str(x.get("id")) for x in live.get("songsList", [])})
        if extra:
            log(f"    ! {extra} song(s) only in the local copy, no sync record: taking the live index")
        result = live
    else:
        result = merge3(snap, local, live)
    if local != result:
        if INDEX_PATH.exists():
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            (BACKUP_DIR / f"music-index.json.bak-{time.strftime('%Y%m%d-%H%M%S')}-presync").write_bytes(INDEX_PATH.read_bytes())
        _write_index(result)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_SNAPSHOT.write_text(json.dumps(live, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    return True

def merge3(base: dict, mine: dict, live: dict) -> dict:
    """Apply what `mine` changed relative to `base` onto `live`, entry by entry."""
    def by(c): return {str(s.get("id")): s for s in c.get("songsList", [])}
    b, m, l = by(base), by(mine), by(live)
    out = dict(live)
    songs = []
    for sid, s in l.items():
        if sid in b and sid not in m:
            continue                                   # I removed it
        songs.append(m[sid] if sid in m and m[sid] != b.get(sid) else s)
    songs += [s for sid, s in m.items() if sid not in b and sid not in l]   # I added it
    out["songsList"] = songs
    for k, v in mine.items():                          # top-level keys I changed
        if k != "songsList" and v != base.get(k):
            out[k] = v
    return out

def load_index() -> dict:
    sync_from_live(required=False)
    cat = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    _loaded_digest[id(cat)] = _index_digest()
    # The merge base is what was LIVE when this command loaded — not the local
    # copy, which may already hold edits of ours that live has not seen yet.
    _loaded_base[id(cat)] = _read_json(LIVE_SNAPSHOT) or json.loads(json.dumps(cat))
    return cat

def index_by_id(cat: dict) -> dict:
    return {str(s.get("id")): s for s in cat.get("songsList", [])}

def next_id(cat: dict) -> str:
    return str(max(int(s["id"]) for s in cat["songsList"] if str(s.get("id")).isdigit()) + 1)

def load_song(cid) -> dict | None:
    """Local copy if present, else the live CDN copy. Legacy songs only exist
    remotely, and silently skipping them is how index and detail drift apart."""
    f = song_filename(cid)
    local = SONGS_DIR / f
    if local.exists():
        try:
            return json.loads(local.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(urllib.request.urlopen(
            f"{CDN}/musicdata/songs/{f}?v={int(time.time())}", timeout=30).read())
    except Exception:
        return None

def write_song(cid, doc: dict) -> str:
    f = song_filename(cid)
    SONGS_DIR.mkdir(parents=True, exist_ok=True)
    (SONGS_DIR / f).write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return f"songs/{f}"

def save_index(cat: dict, label: str = "", force: bool = False) -> Path:
    """Write the index back, refusing to overwrite someone else's changes.

    Every command does load -> mutate -> save on the WHOLE index, so two running
    together is last-writer-wins on the entire catalogue. That is not theoretical:
    a `fix producers` run that had loaded the index before `dedupe` ran wrote its
    stale copy back at the end and silently restored all seven duplicates,
    including a song with no stems. Comparing against the digest taken at load
    turns that into a refusal instead of silent data loss.
    """
    was = _loaded_digest.get(id(cat))
    if was and not force and _index_digest() != was:
        raise RuntimeError(
            "the index changed on disk since this command loaded it — another "
            "command wrote to it. Re-run this one; do not force, or you will "
            "revert whatever the other command did.")
    base, live = _loaded_base.get(id(cat)), None
    if base is not None:
        try:
            live = live_index()
            if live != base:
                merged = merge3(base, cat, live)
                cat.clear(); cat.update(merged)        # the caller keeps working on the merged copy
                log(f"    merged onto the live index ({len(cat['songsList'])} songs)")
        except Exception as e:
            log(f"    ! live index unreachable at save ({e}); saving the local copy")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # Backups live HERE, never next to the index: the app bundles musicdata, so a
    # backup written beside music-index.json shipped inside the app (54 of them had
    # piled up to 121 MB of a ~175 MB install). The app's build now refuses to
    # build if anything but the catalogue itself appears in musicdata.
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"music-index.json.bak-{stamp}{'-' + label if label else ''}"
    backup.write_bytes(INDEX_PATH.read_bytes())
    tmp = INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cat, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, INDEX_PATH)      # atomic
    _loaded_digest[id(cat)] = _index_digest()
    if live is not None:                 # we just saw live: that is the base from here on
        _loaded_base[id(cat)] = live
        LIVE_SNAPSHOT.write_text(json.dumps(live, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    return backup

def publish(files: list[str], expect_ids: list[str] | None = None) -> bool:
    """Push the named musicdata files + the index, then prove it landed."""
    files = [f for f in dict.fromkeys(files) if f]
    fd, lp = tempfile.mkstemp(suffix=".txt"); os.close(fd)
    # trailing newline is load-bearing — see module docstring
    Path(lp).write_text("\n".join(files + ["music-index.json"]) + "\n", encoding="utf-8")
    # Six hours, not forty minutes: the lyrics backfill pushed 3 800 files and
    # the old limit killed the WAIT (not the upload — the files landed) and
    # ended a clean run with a traceback instead of the verification below.
    r = subprocess.run(["bash", str(REPO / "scripts/upload-catalog.sh"), "--files-from", lp],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=6 * 3600)
    for line in (r.stdout or "").strip().splitlines():
        if "done:" in line or "purge:" in line:
            log("    " + line.strip())
    if r.returncode != 0:
        log(f"    ✗ upload-catalog.sh exited {r.returncode}: {(r.stderr or '')[-200:]}")
    ok = True
    try:
        live = json.loads(urllib.request.urlopen(
            f"{CDN}/musicdata/music-index.json?v={int(time.time())}",
            timeout=90).read().decode("utf-8", "replace"))
        have = {str(s.get("id")) for s in live.get("songsList", [])}
        log(f"    live index: {len(live.get('songsList', []))} songs")
        for cid in (expect_ids or []):
            if str(cid) not in have:
                log(f"    ✗ id {cid} is NOT in the live index"); ok = False
    except Exception as e:
        log(f"    ! could not verify the live index ({e})"); ok = False
    if ok and r.returncode == 0:
        mark_published()
    return ok

def head_ok(url: str) -> bool:
    try:
        req = urllib.request.Request(url + f"?v={int(time.time())}", method="HEAD")
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status == 200
    except Exception:
        return False

def stem_urls(doc: dict) -> dict:
    """The URLs the APP will actually load, with the index's stemFormat applied.
    Never assume catalogtracks/<uuid>/ — legacy songs use '<Artist - Title>/'."""
    out = {}
    for s in (doc.get("stems") or []):
        if s.get("id") in STEMS and s.get("url"):
            out[s["id"]] = re.sub(r"\.(mp3|m4a)$", ".m4a", s["url"])
    return out

def remove_from_index(cid) -> None:
    """Drop a song from the index.

    The per-song JSON and the stems on the CDN are LEFT ALONE: removing an index
    entry is reversible, deleting audio is not, and an orphaned file costs
    nothing. Only the listing changes.
    """
    cat = load_index()
    before = len(cat["songsList"])
    cat["songsList"] = [s for s in cat["songsList"] if str(s.get("id")) != str(cid)]
    if len(cat["songsList"]) == before:
        raise KeyError(f"id {cid} not in the index")
    save_index(cat, f"remove-{cid}")

def relabel(cid, *, title=None, artist=None, cover=None, year=None, album=None,
            bpm=None, key=None, producers=None, writers=None, label=None,
            duration=None, drop_fallback=False) -> list[str]:
    """Correct one song's metadata in BOTH the index entry and its detail file.

    This is the reusable form of the fix applied to id 9 (Plastic Love ->
    Namida no One Sided Love) and id 3644 (presidential -> MAKE A SOUND):
    the audio is fine, the labelling is not. Returns the files to publish.
    """
    cat = load_index()
    entry = index_by_id(cat).get(str(cid))
    if entry is None:
        raise KeyError(f"id {cid} not in the index")
    doc = load_song(cid) or {"id": str(cid)}

    def setboth(k_index, k_song, v, mmss_for_index=False):
        if v is None:
            return
        entry[k_index] = fmt_mmss(v) if mmss_for_index else v
        doc[k_song] = v

    if title is not None:
        entry["title"] = title; doc["title"] = title
    if artist is not None:
        entry["artist"] = artist; doc["artist"] = artist
    if cover is not None:
        entry["coverArt"] = cover; doc["coverArt"] = cover
    if bpm is not None:
        entry["bpm"] = bpm; doc["bpm"] = bpm
    if key is not None:
        entry["key"] = key; doc["key"] = key
    if producers is not None:
        entry["producers"] = producers; doc["producers"] = producers
    if writers is not None:
        entry["writers"] = writers; doc["writers"] = writers
    if year is not None:
        doc["year"] = str(year)
    if album is not None:
        doc["album"] = album
    if label is not None:
        doc["recordLabel"] = label
    if duration is not None:
        entry["duration"] = fmt_mmss(duration)
        doc["duration"] = int(duration)
        doc["displayDuration"] = fmt_mmss(duration)
    if drop_fallback:
        entry.pop("coverArtFallback", None); doc.pop("coverArtFallback", None)

    files = [write_song(cid, doc)]
    save_index(cat, f"relabel-{cid}")
    return files

def fmt_mmss(seconds) -> str:
    s = int(round(float(seconds)))
    return f"{s // 60}:{s % 60:02d}"
