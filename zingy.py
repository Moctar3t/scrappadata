#!/usr/bin/env python3
"""
zingy.py — ONE entry point for the catalogue.

Before this, the same knowledge lived in seven places: ingest_gpu.py,
fix_identities.py, apply_romaji.py, catalog_audit.py, build_add_csv.py, a
detector that only existed on the VPS, and one fix typed straight into a shell.
Each repair re-derived how to edit and publish the catalogue, which is exactly
how a step gets missed.

  zingy.py add      --csv F | --album "Artist/Album" | --track "Artist - Title"  [--line replicate]
  zingy.py audit    [--deep N]        every health check, read-only
  zingy.py fix      romaji | artists | covers | producers | bpmkey | vibe
  zingy.py spotify  login | pull [--apply] | status     the playlist inbox
  zingy.py dedupe   [--apply]         resolve songs the app hides as duplicates
  zingy.py repair   [--apply]         re-split catalogue songs that have no stems
  zingy.py relabel  ID --title ... --artist ...      correct a mislabelled song
  zingy.py verify   [--ids ...]       re-check songs against the LIVE CDN
  zingy.py status                     ingest manifest state

Every mutating command loads the whole index, so `save_index` refuses to write
if the file changed underneath — two commands at once would otherwise be
last-writer-wins across the entire catalogue.

Ingest itself still lives in ingest_gpu.py — this drives it rather than
duplicating it, so there is one implementation of the gate.
"""
from __future__ import annotations
import argparse, json, re, struct, subprocess, sys, time, urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import catalog_lib as cl                     # noqa: E402
from romaji import to_romaji, has_japanese, romanize_artist  # noqa: E402
from ingest_gpu import title_matches, atoks  # noqa: E402  (one gate, used everywhere)

def log(m): print(m, flush=True)
def run(args): return subprocess.run([sys.executable, str(HERE / "ingest_gpu.py")] + args).returncode

# ── add ─────────────────────────────────────────────────────────────────────
def cmd_add(a):
    """Acquire authoritative durations, then run the ingest stages in order."""
    csv_path = a.csv
    if a.album or a.track:
        import build_add_csv as bac
        spec = {"albums": [], "tracks": []}
        if a.album:
            for s in a.album:
                artist, _, name = s.partition("/")
                spec["albums"].append((artist.strip(), name.strip()))
        if a.track:
            for s in a.track:
                artist, _, title = s.partition(" - ")
                spec["tracks"].append((artist.strip(), title.strip(), None))
        csv_path = str(bac.build(spec))
        log(f"  built {csv_path}")
    if not csv_path:
        sys.exit("  add needs --csv, --album or --track")
    ingest_csv(csv_path, a.line)

def ingest_csv(csv_path: str, line: str = "gpu") -> None:
    """Every stage, in order, for the songs of one Exportify-shaped CSV. `line`:
    gpu (the home box) or replicate (the VPS + Replicate, no home GPU); resolve
    and produce touch only that line's tracks."""
    for stage in (["prepare", "--csv", csv_path, "--line", line], ["resolve", "--line", line],
                  ["produce", "--line", line], ["upload"], ["enrich"], ["vibe"], ["publish"], ["report"]):
        log(f"\n══ {stage[0]} ══")
        if run(stage) != 0 and stage[0] in ("prepare", "resolve"):
            sys.exit(f"  {stage[0]} failed — stopping before the long work")

# ── spotify: the playlist inbox ─────────────────────────────────────────────
def cmd_spotify(a):
    import spotify_intake as si
    try:
        if a.action == "login":
            si.login()
        elif a.action == "use":
            if not a.arg:
                sys.exit('  use needs the playlist: zingy.py spotify use "<link or name>"')
            si.use(a.arg)
        elif a.action == "pull":
            log(f"── spotify pull {si.now_iso()} ──")
            # The inbox never touches the home GPU: Replicate line, start to end.
            si.pull(a.apply, lambda path: ingest_csv(path, "replicate"))
        elif a.action == "status":
            si.status()
        elif a.action == "schedule":
            if a.arg not in ("on", "off"):
                sys.exit("  schedule on | off")
            si.schedule(a.arg == "on")
    except si.SpotifyError as e:
        sys.exit(f"  ! {e}")

# ── audit ───────────────────────────────────────────────────────────────────
def primary_artist(s: str) -> str:
    s = re.sub(r"\(feat.*?\)|\[feat.*?\]|feat\.?|ft\.?|featuring", "", (s or "").lower())
    return re.sub(r"[^a-z0-9]", "", re.split(r"[;,&]| x | and |/", s)[0])

def audit(deep: int = 0) -> dict:
    cat = cl.load_index()
    songs = cat["songsList"]
    rep = {"total": len(songs)}
    log(f"catalogue: {len(songs)} songs\n")

    # 1. Japanese titles — must always be zero; romanisation is automatic at
    #    ingest, so anything here means a song predates it or the lookup failed.
    jp = [s for s in songs if has_japanese(s.get("title", ""))]
    rep["japanese"] = [s["id"] for s in jp]
    ja = [s for s in songs if has_japanese(s.get("artist", ""))]
    rep["japanese_artists"] = [s["id"] for s in ja]
    log(f"  [titles]  Japanese-script titles : {len(jp)}")
    log(f"  [titles]  Japanese-script artists: {len(ja)}")
    for s in jp[:8]:
        log(f"              {s['id']:>5} {s.get('artist','')[:22]:22s} {s['title']}")

    # 2. covers shared across DIFFERENT lead artists — album art is not shared
    #    between artists, so this is compilation art or a generic image.
    by_cover = defaultdict(list)
    for s in songs:
        if s.get("coverArt"):
            by_cover[s["coverArt"]].append(s)
    shared = [(u, g) for u, g in by_cover.items()
              if len({primary_artist(x.get("artist", "")) for x in g if x.get("artist")}) >= 2]
    shared.sort(key=lambda x: -len(x[1]))
    rep["shared_covers"] = [{"url": u, "ids": [x["id"] for x in g]} for u, g in shared]
    log(f"  [covers]  shared across artists  : {len(shared)} image(s), "
        f"{sum(len(g) for _, g in shared)} songs")
    for u, g in shared[:6]:
        log(f"              {len(g)} songs  {u[:66]}")
        for x in g[:2]:
            log(f"                 {x['id']:>5} {x.get('artist','')[:24]:24s} {x.get('title','')[:28]}")

    # 3. low-res Genius art = the artist image it falls back to when it has no
    #    song art. Real song art comes back 1000x1000.
    lowres = []
    for s in songs:
        m = re.search(r"\.(\d{2,4})x(\d{2,4})x\d", s.get("coverArt") or "")
        if m and int(m.group(1)) < 500:
            lowres.append(s)
    rep["lowres_covers"] = [s["id"] for s in lowres]
    log(f"  [covers]  low-res artist images  : {len(lowres)}")

    # 4. missing cover entirely
    nocover = [s for s in songs if not s.get("coverArt")]
    rep["no_cover"] = [s["id"] for s in nocover]
    log(f"  [covers]  no cover at all        : {len(nocover)}")

    # 5. duplicate (title, lead artist) — the app dedups these at load, so a
    #    duplicate is a song silently missing from the library.
    seen = defaultdict(list)
    for s in songs:
        seen[(re.sub(r"[^a-z0-9]", "", s.get("title", "").lower()),
              primary_artist(s.get("artist", "")))].append(s["id"])
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    rep["duplicates"] = [{"key": list(k), "ids": v} for k, v in dupes.items()]
    log(f"  [dupes]   same title+lead artist : {len(dupes)} group(s)")
    for k, v in list(dupes.items())[:6]:
        log(f"              {v}  {k[1]} — {k[0][:34]}")

    # 6. fields the app shows but that are absent
    for field, lbl in (("bpm", "bpm (MANDATORY)"), ("key", "key (MANDATORY)"), ("producers", "producers"),
                       ("genres", "genres")):
        miss = [s["id"] for s in songs if not s.get(field)]
        rep[f"missing_{field}"] = miss
        log(f"  [meta]    missing {lbl:10s}     : {len(miss)}")

    if deep:
        log(f"\n  [deep] HTTP-checking {deep} random songs against the live CDN …")
        import random
        random.seed(11)
        broken = []
        for s in random.sample(songs, min(deep, len(songs))):
            doc = cl.load_song(s["id"])
            if not doc:
                broken.append((s["id"], "no detail JSON")); continue
            urls = cl.stem_urls(doc)
            if len(urls) < 4:
                broken.append((s["id"], f"only {len(urls)} stems declared")); continue
            bad = [k for k, u in urls.items() if not cl.head_ok(u)]
            if bad:
                broken.append((s["id"], f"stems not serving: {bad}")); continue
            # A TRUNCATED stem still returns 200. Plastic Love shipped with a bass
            # stem 98s shorter than its siblings and every reachability check passed,
            # so compare byte sizes: the four stems of one song are always close.
            sizes = {}
            for k, u in urls.items():
                try:
                    req = urllib.request.Request(u + f"?v={int(time.time())}", method="HEAD")
                    with urllib.request.urlopen(req, timeout=25) as r:
                        sizes[k] = int(r.headers.get("Content-Length") or 0)
                except Exception:
                    pass
            if len(sizes) == 4 and max(sizes.values()) > 0:
                hi = max(sizes.values())
                short = [k for k, v in sizes.items() if v < hi * 0.75]
                if short:
                    broken.append((s["id"], f"stem much shorter than siblings: {short}"))
        rep["broken"] = broken
        log(f"  [deep] unplayable: {len(broken)}/{deep}")
        for i, why in broken[:10]:
            log(f"           {i}: {why}")
    return rep

def cmd_audit(a):
    rep = audit(a.deep)
    out = HERE / "catalog_audit_report.json"
    out.write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"\nwrote {out.name}")

# ── fix ─────────────────────────────────────────────────────────────────────
def fix_romaji(apply: bool):
    cat = cl.load_index()
    songs = cat["songsList"]
    jp = [s for s in songs if has_japanese(s.get("title", ""))]
    if not jp:
        log("  no Japanese titles — nothing to do."); return
    files, ids = [], []
    for s in jp:
        rom, src = to_romaji(s["title"], s.get("artist", ""))
        if not rom or rom == s["title"] or has_japanese(rom):
            log(f"    ! unresolved {s['id']}: {s['title']}"); continue
        if rom == rom.lower():
            rom = " ".join(w.capitalize() for w in rom.split())
        log(f"    {s['id']:>5} {s['title'][:26]:26s} -> {rom[:34]:34s} [{src}]")
        if apply:
            s["title"] = rom
            doc = cl.load_song(s["id"])
            if doc:
                doc["title"] = rom
                files.append(cl.write_song(s["id"], doc))
            ids.append(s["id"])
        time.sleep(0.2)
    if apply and ids:
        cl.save_index(cat, "romaji")
        cl.publish(files, ids)

def fix_covers(apply: bool, limit: int):
    """Replace compilation / generic art with the artist's own release art.

    Only acts where iTunes returns a release BY THIS ARTIST that is not a
    Various-Artists compilation — the same rule the ingest now applies.
    """
    import urllib.parse, urllib.request
    cat = cl.load_index()
    songs = cat["songsList"]
    by_cover = defaultdict(list)
    for s in songs:
        if s.get("coverArt"):
            by_cover[s["coverArt"]].append(s)
    targets = []
    for u, g in by_cover.items():
        if len({primary_artist(x.get("artist", "")) for x in g if x.get("artist")}) >= 2:
            targets += g
    for s in songs:
        m = re.search(r"\.(\d{2,4})x(\d{2,4})x\d", s.get("coverArt") or "")
        if m and int(m.group(1)) < 500:
            targets.append(s)
    seen, uniq = set(), []
    for s in targets:
        if s["id"] not in seen:
            seen.add(s["id"]); uniq.append(s)
    uniq = uniq[:limit] if limit else uniq
    log(f"  {len(uniq)} song(s) with suspect cover art")

    files, ids = [], []
    for s in uniq:
        art = a_artist = None
        try:
            q = urllib.parse.quote(f"{s.get('artist','')} {s.get('title','')}")
            res = json.loads(urllib.request.urlopen(
                f"https://itunes.apple.com/search?term={q}&entity=song&limit=6",
                timeout=20).read().decode("utf-8", "replace")).get("results", [])
        except Exception:
            res = []
        want = {re.sub(r'[^a-z0-9]', '', p) for p in re.split(r"[;,&]", (s.get("artist") or "").lower()) if p.strip()}
        cands = []
        for r in res:
            an = (r.get("artistName") or "")
            if an.strip().lower() == "various artists":
                continue
            got = {re.sub(r'[^a-z0-9]', '', p) for p in re.split(r"[;,&]", an.lower()) if p.strip()}
            if want and got and not (want & got):
                continue
            # The result must be THIS SONG, not merely the same artist. Without
            # this, three different Lancey Foux tracks all inherited one single's
            # sleeve — the same "right artist, wrong song" failure that put
            # Namida no One-sided Love in the catalogue as Plastic Love.
            if not title_matches(s.get("title", ""), r.get("trackName") or ""):
                continue
            if r.get("artworkUrl100"):
                cands.append(r)
        if not cands:
            continue
        cands.sort(key=lambda r: (r.get("releaseDate") or "9999"))   # original, not reissue
        art = cands[0]["artworkUrl100"].replace("100x100bb", "1000x1000bb")
        a_artist = cands[0].get("collectionName")
        if art == s.get("coverArt"):
            continue
        log(f"    {s['id']:>5} {s.get('artist','')[:20]:20s} {s.get('title','')[:24]:24s} -> {str(a_artist)[:28]}")
        if apply:
            s["coverArt"] = art
            doc = cl.load_song(s["id"])
            if doc:
                doc["coverArt"] = art
                files.append(cl.write_song(s["id"], doc))
            ids.append(s["id"])
        time.sleep(0.35)
    if apply and ids:
        cl.save_index(cat, "covers")
        cl.publish(files, ids)
    log(f"  {'applied' if apply else 'would change'} {len(ids) if apply else '…'} cover(s)")

def fix_artists(apply: bool):
    """Romanise Japanese ARTIST names. Titles were done first and bylines were
    missed — the requirement is that no Japanese script reaches the app."""
    cat = cl.load_index()
    songs = cat["songsList"]
    jp = [s for s in songs if has_japanese(s.get("artist", ""))]
    if not jp:
        log("  no Japanese artist names — nothing to do."); return
    files, ids = [], []
    for s in jp:
        rom, src = romanize_artist(s["artist"])
        if not rom or rom == s["artist"] or has_japanese(rom):
            log(f"    ! unresolved {s['id']}: {s['artist']}"); continue
        log(f"    {s['id']:>5} {s['artist'][:30]:30s} -> {rom[:34]:34s} [{src}]")
        if apply:
            s["artist"] = rom
            doc = cl.load_song(s["id"])
            if doc:
                doc["artist"] = rom
                files.append(cl.write_song(s["id"], doc))
            ids.append(s["id"])
    if apply and ids:
        cl.save_index(cat, "artists")
        cl.publish(files, ids)
    log(f"  {'applied' if apply else 'would change'} {len(ids) if apply else len(jp)} artist(s)")

def fix_producers(apply: bool, limit: int):
    """Recover producer credits that Genius HAS but the catalogue lacks.

    Measured on a 50-song sample of the 504 missing: 58% have no Genius page at
    all, 34% have a page with no credits filled in, and only ~8% are genuinely
    recoverable. So this is a small, targeted pass — not a fix for the majority,
    which is simply absent data.
    """
    from ingest_gpu import pick_genius_hit, genius_get
    cat = cl.load_index()
    songs = [s for s in cat["songsList"] if not s.get("producers")]
    songs = songs[:limit] if limit else songs
    log(f"  checking {len(songs)} song(s) with no producer credit")
    files, ids = [], []
    for s in songs:
        t = {"title": s.get("title", ""), "artist": s.get("artist", "")}
        try:
            d = genius_get("/search", {"q": f"{t['artist']} {t['title']}"})
            hit = pick_genius_hit(t, ((d or {}).get("response") or {}).get("hits") or [])
        except Exception:
            continue
        if not hit:
            continue
        full = genius_get(f"/songs/{hit['id']}", {"text_format": "plain"})
        song = ((full or {}).get("response") or {}).get("song") or {}
        prod = [x.get("name") for x in (song.get("producer_artists") or []) if x.get("name")]
        writ = [x.get("name") for x in (song.get("writer_artists") or []) if x.get("name")]
        if not prod:
            continue
        log(f"    {s['id']:>5} {s.get('artist','')[:20]:20s} {s.get('title','')[:24]:24s} -> {', '.join(prod)[:40]}")
        if apply:
            s["producers"] = ", ".join(prod)
            if writ and not s.get("writers"):
                s["writers"] = ", ".join(writ)
            doc = cl.load_song(s["id"])
            if doc:
                doc["producers"] = s["producers"]
                if writ and not doc.get("writers"):
                    doc["writers"] = ", ".join(writ)
                files.append(cl.write_song(s["id"], doc))
            ids.append(s["id"])
        time.sleep(0.25)
    if apply and ids:
        cl.save_index(cat, "producers")
        cl.publish(files, ids)
    log(f"  recovered {len(ids)} credit(s)")

def fix_bpmkey(apply: bool, limit: int):
    """Backfill BPM + key for catalogue songs that have neither, by analysing
    their published stems ON THE VPS.

    Why it is ever missing: an Exportify CSV carries Spotify's Tempo/Key, but
    Spotify closed the audio-features endpoint to new apps in Nov 2024, so a song
    added by album or track lookup arrives with none. Genius has no BPM/key at
    all — it only holds credits — so measurement is the only route.

    New ingests no longer need this: `produce` measures on the GPU box while the
    decoded source is still there. This is for songs already published.
    """
    import subprocess, sys as _s
    cat = cl.load_index()
    need = [x for x in cat["songsList"] if not x.get("bpm") or not x.get("key")]
    need = need[:limit] if limit else need
    if not need:
        log("  every song has bpm and key."); return
    log(f"  {len(need)} song(s) missing bpm/key")

    targets = []
    for x in need:
        doc = cl.load_song(x["id"])
        if not doc:
            log(f"    ! {x['id']}: no detail JSON, skipping"); continue
        urls = cl.stem_urls(doc)
        if len(urls) < 4:
            log(f"    ! {x['id']}: only {len(urls)} stems declared, skipping"); continue
        targets.append({"id": str(x["id"]), "artist": x.get("artist"),
                        "title": x.get("title"), "stems": urls})
    if not targets:
        log("  nothing analysable."); return
    if not apply:
        log(f"  would analyse {len(targets)} song(s) on the VPS")
        log("  (dry run — pass --apply)"); return

    tf = HERE / "bpmkey_targets.json"
    tf.write_text(json.dumps(targets, indent=1, ensure_ascii=False), encoding="utf-8")
    vps = "root@204.168.236.140"
    for f in (HERE / "vps_analyze.py", tf):
        subprocess.run(["scp", "-o", "ConnectTimeout=20", str(f), f"{vps}:/root/"],
                       check=True, timeout=300)
    log(f"  analysing {len(targets)} song(s) on the VPS …")
    subprocess.run(["ssh", "-o", "ConnectTimeout=20", vps,
                    "cd /root && /var/www/zingy-api/analysis/venv/bin/python "
                    "vps_analyze.py bpmkey_targets.json bpmkey_results.jsonl"],
                   timeout=14400)
    res = HERE / "bpmkey_results.jsonl"
    subprocess.run(["scp", "-o", "ConnectTimeout=20", f"{vps}:/root/bpmkey_results.jsonl",
                    str(res)], check=True, timeout=300)

    by_id = cl.index_by_id(cat)
    files, ids, failed = [], [], 0
    for ln in res.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        if not r.get("bpm") or not r.get("key"):
            failed += 1; continue
        e = by_id.get(str(r["id"]))
        if not e:
            continue
        e["bpm"], e["key"] = r["bpm"], r["key"]
        doc = cl.load_song(r["id"])
        if doc:
            doc["bpm"], doc["key"] = r["bpm"], r["key"]
            files.append(cl.write_song(r["id"], doc))
        ids.append(str(r["id"]))
    if ids:
        cl.save_index(cat, "bpmkey")
        cl.publish(files, ids)
    log(f"  filled {len(ids)} song(s); {failed} could not be analysed")

def fix_vibe(apply: bool, limit: int):
    """The six vibe numbers (La boîte bleue) for published songs that have none.

    Spotify's numbers come with an Exportify CSV; a song added by playlist, album
    or track lookup has none, and the app's AUTO taste, the visualiser's mood and
    the Oracle all read them. Scored on the VPS from the published stems. The
    index is loaded again after the scoring (it can take half an hour), so an edit
    made meanwhile is not written over, and a value already there never is.
    """
    import vibe
    cat = cl.load_index()
    need = [x for x in cat["songsList"] if vibe.missing(x)]
    need = need[:limit] if limit else need
    if not need:
        log("  every song has its vibe numbers."); return
    targets = []
    for x in need:
        doc = cl.load_song(x["id"])
        urls = cl.stem_urls(doc) if doc else {}
        if len(urls) < 4:
            log(f"    ! {x['id']}: {len(urls)} stems declared, skipping"); continue
        targets.append({"id": str(x["id"]), "stems": urls})
    log(f"  {len(need)} song(s) without vibe numbers, {len(targets)} with stems to score")
    if not apply:
        for x in need[:12]:
            log(f"    {x['id']:>5} {x.get('artist','')[:24]:24s} {x.get('title','')[:30]}")
        log(f"  would score {len(targets)} on the VPS (~{len(targets) * 14 // 60 + 1} min)"); return
    got = vibe.score_on_vps(targets, "backfill")
    cat = cl.load_index()
    by_id = cl.index_by_id(cat)
    ids, failed = [], 0
    for cid, r in got.items():
        e = by_id.get(cid)
        if e is None or "error" in r:
            failed += 1; continue
        for k in vibe.VIBE:
            if e.get(k) is None:
                e[k] = r[k]
        ids.append(cid)
    if ids:
        cl.save_index(cat, "vibe")
        cl.publish([], ids)
    vibe.finish("backfill")
    log(f"  scored {len(ids)} song(s); {failed} could not be")

class LrclibUnavailable(RuntimeError):
    """LRCLIB kept failing after retries — the run must STOP, not record misses.

    The first full pass silently logged 200 consecutive "no confident match"
    around song 2 800: a rate-limit window, swallowed by a bare `except` and
    counted as songs that have no lyrics. A miss is a fact about the song; an
    error is a fact about the network, and the two must never share a column.
    """

def _lrclib(path: str, params: dict):
    """One LRCLIB call. 404 is a miss (None); 429/5xx/timeouts back off and
    retry, and raise `LrclibUnavailable` if they persist."""
    import urllib.error, urllib.parse
    url = f"https://lrclib.net{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "zingy-catalogue (dime.systems)"})
    delay = 5
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code not in (429, 500, 502, 503, 504):
                raise LrclibUnavailable(f"HTTP {e.code} on {path}")
            log(f"    ! lrclib HTTP {e.code} — waiting {delay}s (attempt {attempt + 1}/6)")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            log(f"    ! lrclib {type(e).__name__}: {str(e)[:60]} — waiting {delay}s (attempt {attempt + 1}/6)")
        time.sleep(delay)
        delay = min(120, delay * 2)
    raise LrclibUnavailable(f"gave up on {path} after 6 attempts")

def query_artist(a: str) -> str:
    """The one name LRCLIB's artist_name param wants.

    It is not a credit-string matcher: querying it with "Travis Scott (feat.
    Young Thug & M.I.A.), M.I.A., Young Thug" returns zero hits on both /get
    and /search, where "Travis Scott" alone finds the song straight away.
    """
    a = re.sub(r"\(feat.*?\)|\[feat.*?\]", "", a or "", flags=re.I)
    return re.split(r"[;,&/]| x | and | feat\.?| ft\.?| featuring", a, flags=re.I)[0].strip()

def _range(url: str, a: int, b: int) -> bytes:
    req = urllib.request.Request(url, headers={"Range": f"bytes={a}-{b}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def stem_seconds(url: str):
    """Length of one stem file, read from its MP4 header with two small range
    requests (the file's own ftyp/mdat header gives where moov sits; moov's mvhd
    holds timescale and duration). None when the file is not an MP4 we can read."""
    head = _range(url, 0, 4095)
    i, moov = 0, None
    while i + 8 <= len(head):
        size, typ = struct.unpack(">I4s", head[i:i + 8])
        if size == 1:
            size = struct.unpack(">Q", head[i + 8:i + 16])[0]
        if typ == b"moov":
            moov = i; break
        if typ == b"mdat":
            moov = i + size; break
        if size < 8:
            return None
        i += size
    if moov is None:
        return None
    m = _range(url, moov, moov + 4095)
    j = m.find(b"mvhd")
    if j < 0:
        return None
    if m[j + 4] == 0:
        ts, du = struct.unpack(">II", m[j + 16:j + 24])
    else:
        ts, du = struct.unpack(">IQ", m[j + 24:j + 36])
    return round(du / ts, 3) if ts else None

def audio_seconds(doc: dict):
    """How long the audio the app PLAYS is: the vocal stem (the one lyrics are
    heard against), else any stem. Cached on the detail doc as `audioDuration`.

    Not the catalogue's `duration`: that comes from the metadata source and names
    a release, while the stems may come from another edit of it. 78 songs were
    off by more than 4 s, American Boy by 42 s (stems from the video edit, lyrics
    matched to the 4:44 album version by the catalogue's duration)."""
    if doc.get("audioDuration"):
        return doc["audioDuration"]
    urls = cl.stem_urls(doc)
    for k in ("vocals", "other", "drums", "bass"):
        if urls.get(k):
            try:
                secs = stem_seconds(urls[k])
            except Exception:
                secs = None
            if secs:
                doc["audioDuration"] = secs
                return secs
    return None

def best_lrclib_match(title: str, artist: str, want_duration: int):
    """Exact lookup first; a scored search only when that misses.

    /get already enforces its own +-2s duration tolerance, but a duration match
    is not a title match (a cover or a live take can share a runtime) — see
    title_matches' own docstring on id 9 — so every hit, exact or searched, is
    re-gated the same way Genius credits are in fix_producers. /search carries
    no duration filter at all, so its candidates also need the closest-runtime
    pick among the ones that pass the gate.
    """
    qa = query_artist(artist)
    hit = _lrclib("/api/get", {"track_name": title, "artist_name": qa,
                                "duration": want_duration} if want_duration else
                  {"track_name": title, "artist_name": qa})
    if (hit and hit.get("syncedLyrics") and not hit.get("instrumental")
            and title_matches(title, hit.get("trackName", ""))):
        return hit
    cands = _lrclib("/api/search", {"track_name": title, "artist_name": qa}) or []
    want_a = atoks(artist)
    best, best_diff = None, 1e9
    for c in cands:
        if not c.get("syncedLyrics") or c.get("instrumental"):
            continue
        if not title_matches(title, c.get("trackName", "")):
            continue
        ca = atoks(c.get("artistName", ""))
        # atoks splits on punctuation, not plain whitespace, so LRCLIB's
        # unparenthesized "Estelle feat. Kanye West" collapses to one token
        # ("estellekanyewest") after "feat." is stripped, and never equals our
        # own {"estelle"} by set intersection. Substring is enough here because
        # title_matches has already done the hard work of confirming the song.
        if want_a and ca and not any(w in c2 or c2 in w for w in want_a for c2 in ca):
            continue
        diff = abs((c.get("duration") or 0) - want_duration) if want_duration else 999
        if diff <= 4 and diff < best_diff:
            best, best_diff = c, diff
    return best

def fix_index_credits(apply: bool, limit: int):
    """Copy each song's year and record label from its detail doc into the index.

    The index is what the app loads whole and scores every song from; year and label
    only lived in the per-song detail docs, so the taste engine's era and label
    affinities learned from the song playing and could never score a candidate.
    New ingests write them to the index directly (`build_index_entry`).
    """
    cat = cl.load_index()
    songs = cat["songsList"][:limit] if limit else cat["songsList"]
    changed = 0
    for s in songs:
        doc = cl.load_song(s["id"]) or {}
        year = str(doc.get("year") or "")[:4]
        label = (doc.get("recordLabel") or "").strip()
        if year.isdigit() and s.get("year") != year:
            s["year"] = year; changed += 1
        if label and s.get("recordLabel") != label:
            s["recordLabel"] = label; changed += 1
    have_year = sum(1 for s in cat["songsList"] if s.get("year"))
    have_label = sum(1 for s in cat["songsList"] if s.get("recordLabel"))
    log(f"  {changed} field(s) filled; index now has year on {have_year}, label on {have_label} "
        f"of {len(cat['songsList'])}")
    if not apply:
        log("  (dry run — pass --apply)"); return
    if changed:
        cl.save_index(cat, "index-credits")
        cl.publish([], [])

LYRIC_VERSION_TOLERANCE = 4.0   # seconds; LRCLIB's own /get tolerance is 2

def fix_lyrics_version(apply: bool, limit: int):
    """Every song's synced lyrics must be timed for the audio the app plays.

    Lyrics timed for another edit (a video version, a radio edit, an extended
    mix) are right until the first section the edits differ in, then wrong for
    the rest of the song, and no constant offset on the app's side can fix that.
    For each song with lyrics: measure the stems (`audioDuration`), learn the
    length the lyrics were timed on (`lyrics.duration`, from LRCLIB by id when an
    older run did not record it), and when they differ by more than the
    tolerance, look for lyrics timed for OUR length; failing that, the synced
    lyrics come off (plain text kept, `hasLyrics` off) rather than stay wrong.
    Report: lyrics_version_report.json.
    """
    cat = cl.load_index()
    songs = [s for s in cat["songsList"] if s.get("hasLyrics")]
    songs = songs[:limit] if limit else songs
    log(f"  checking {len(songs)} song(s) with lyrics against their audio")
    files, ids, report = [], [], {"replaced": [], "dropped": [], "unmeasured": []}
    checked = 0
    for i, s in enumerate(songs):
        doc = cl.load_song(s["id"])
        lyr = (doc or {}).get("lyrics") or {}
        if not doc or not lyr.get("synced"):
            continue
        changed = False
        had = doc.get("audioDuration")
        audio = audio_seconds(doc)
        changed |= audio is not None and had is None
        lrc = lyr.get("duration")
        if lrc is None and lyr.get("lrclibId"):
            try:
                got = _lrclib(f"/api/get/{lyr['lrclibId']}", {}) or {}
            except LrclibUnavailable as e:
                log(f"  ✗ LRCLIB unavailable at song {i + 1}/{len(songs)} ({e}) — stopping; "
                    f"what was checked so far is kept and published, re-run to resume")
                break
            lrc = got.get("duration")
            if lrc is not None:
                lyr["duration"] = lrc; changed = True
            time.sleep(0.2)
        if audio is None or lrc is None:
            report["unmeasured"].append({"id": s["id"], "title": s.get("title"), "audio": audio, "lrc": lrc})
        elif abs(audio - lrc) > LYRIC_VERSION_TOLERANCE:
            try:
                hit = best_lrclib_match(s.get("title", ""), s.get("artist", ""), round(audio))
            except LrclibUnavailable as e:
                log(f"  ✗ LRCLIB unavailable at song {i + 1}/{len(songs)} ({e}) — stopping")
                break
            row = {"id": s["id"], "title": s.get("title"), "artist": s.get("artist"),
                   "audio": audio, "lrc": lrc, "was": lyr.get("lrclibId")}
            if hit and abs((hit.get("duration") or 0) - audio) <= LYRIC_VERSION_TOLERANCE:
                doc["lyrics"] = {"synced": hit["syncedLyrics"], "plain": hit.get("plainLyrics", ""),
                                  "source": "lrclib", "lrclibId": hit["id"], "duration": hit.get("duration")}
                row["now"] = hit["id"]; row["now_duration"] = hit.get("duration")
                report["replaced"].append(row)
                log(f"    ↻ {s['id']:>5} {s.get('title','')[:28]:28s} audio {audio:6.1f}s, lyrics {lrc}s "
                    f"-> #{hit['id']} ({hit.get('duration')}s)")
            else:
                doc["lyrics"] = {"plain": lyr.get("plain", ""), "source": lyr.get("source"),
                                  "lrclibId": lyr.get("lrclibId"), "duration": lrc,
                                  "syncedDropped": f"timed for {lrc}s, audio is {audio}s, no version for our edit"}
                s["hasLyrics"] = False
                report["dropped"].append(row)
                log(f"    ✗ {s['id']:>5} {s.get('title','')[:28]:28s} audio {audio:6.1f}s, lyrics {lrc}s "
                    f"-> no lyrics for our edit, synced removed")
            changed = True
            time.sleep(0.2)
        checked += 1
        if changed and apply:
            files.append(cl.write_song(s["id"], doc)); ids.append(s["id"])
        if (i + 1) % 250 == 0:
            log(f"    … {i + 1}/{len(songs)}  ({len(report['replaced'])} replaced, "
                f"{len(report['dropped'])} dropped so far)")
    (HERE / "lyrics_version_report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    log(f"  checked {checked}: {len(report['replaced'])} replaced, {len(report['dropped'])} dropped, "
        f"{len(report['unmeasured'])} unmeasured (report: lyrics_version_report.json)")
    if not apply:
        log("  (dry run — pass --apply)"); return
    if ids:
        cl.save_index(cat, "lyrics-version")
        cl.publish(files, ids)

def fix_lyrics(apply: bool, limit: int):
    """Backfill timestamped lyrics from LRCLIB — free, no key, built for this.

    Written to the per-song detail JSON only: synced lyrics run a few KB each
    and the index is loaded whole by the app on every launch. The index only
    gets a `hasLyrics` flag, the same shape as `hasStems`.
    """
    cat = cl.load_index()
    songs = [s for s in cat["songsList"] if not s.get("hasLyrics")]
    songs = songs[:limit] if limit else songs
    log(f"  checking {len(songs)} song(s) without lyrics")
    files, ids, matched, misses = [], [], 0, 0
    for i, s in enumerate(songs):
        doc = cl.load_song(s["id"])
        if not doc:
            misses += 1; continue
        if (doc.get("lyrics") or {}).get("synced"):
            # Written by an earlier run that died before it could save the index
            # or publish — flag it AND put its file back on the publish list.
            matched += 1
            if apply:
                s["hasLyrics"] = True
                files.append(f"songs/{cl.song_filename(s['id'])}")
                ids.append(s["id"])
            continue
        try:
            # Match the edit we PLAY, not the catalogue's release duration.
            want = audio_seconds(doc)
            hit = best_lrclib_match(s.get("title", ""), s.get("artist", ""),
                                    round(want) if want else (doc.get("duration") or 0))
        except LrclibUnavailable as e:
            log(f"  ✗ LRCLIB unavailable at song {i + 1}/{len(songs)} ({e}) — stopping; "
                f"what matched so far is kept and published, re-run to resume")
            break
        if not hit:
            misses += 1
        else:
            matched += 1
            log(f"    {s['id']:>5} {s.get('artist','')[:20]:20s} {s.get('title','')[:26]:26s} "
                f"-> lrclib #{hit['id']} ({hit.get('duration')}s)")
            if apply:
                doc["lyrics"] = {"synced": hit["syncedLyrics"], "plain": hit.get("plainLyrics", ""),
                                  "source": "lrclib", "lrclibId": hit["id"],
                                  "duration": hit.get("duration")}
                s["hasLyrics"] = True
                files.append(cl.write_song(s["id"], doc))
                ids.append(s["id"])
        if (i + 1) % 200 == 0:
            log(f"    … {i + 1}/{len(songs)}  ({matched} matched, {misses} missed so far)")
        time.sleep(0.2)
    if apply and ids:
        cl.save_index(cat, "lyrics")
        cl.publish(files, ids)
    log(f"  {'matched' if apply else 'would match'} {matched}/{len(songs)}; {misses} no confident match")

def cmd_fix(a):
    if a.what == "romaji":
        fix_romaji(a.apply)
    elif a.what == "covers":
        fix_covers(a.apply, a.limit)
    elif a.what == "artists":
        fix_artists(a.apply)
    elif a.what == "producers":
        fix_producers(a.apply, a.limit)
    elif a.what == "bpmkey":
        fix_bpmkey(a.apply, a.limit)
    elif a.what == "vibe":
        fix_vibe(a.apply, a.limit)
    elif a.what == "lyrics":
        fix_lyrics(a.apply, a.limit)
    elif a.what == "lyrics-version":
        fix_lyrics_version(a.apply, a.limit)
    elif a.what == "index-credits":
        fix_index_credits(a.apply, a.limit)
    if not a.apply:
        log("\n(dry run — pass --apply to write and publish)")

def credited(a: str) -> frozenset:
    """Everyone the string credits, featured artists INCLUDED.

    Deliberately not `atoks`, which strips "(feat. X)" to isolate the lead
    artist. Here the question is whether two spellings name the same people, and
    "Batbat (feat. La Honda 19)" names two.
    """
    a = (a or "").lower()
    a = re.sub(r"\b(feat|ft|featuring|with|avec)\b\.?", ",", a)
    parts = re.split(r"[;,&/()\[\]]| x ", a)
    return frozenset(t for t in (re.sub(r"[^a-z0-9]", "", p) for p in parts) if t)

# ── repair ──────────────────────────────────────────────────────────────────
def find_unplayable(songs, check_lengths: bool = False):
    """Songs the app cannot play properly.

    Two defects, not one: a song can declare fewer than 4 stems, OR declare four
    where one is TRUNCATED. The second is invisible to a reachability check —
    Plastic Love served a bass stem 98 seconds shorter than its siblings and
    every URL returned 200 — so sizes are compared when asked.
    """
    out = []
    for s in songs:
        doc = cl.load_song(s["id"])
        if not doc:
            out.append((s, "no detail JSON")); continue
        u = cl.stem_urls(doc)
        if len(u) < 4:
            out.append((s, f"declares {len(u)} stems")); continue
        if not check_lengths:
            continue
        sizes = {}
        for k, url in u.items():
            try:
                req = urllib.request.Request(url + f"?v={int(time.time())}", method="HEAD")
                with urllib.request.urlopen(req, timeout=25) as r:
                    sizes[k] = int(r.headers.get("Content-Length") or 0)
            except Exception:
                pass
        if len(sizes) == 4 and max(sizes.values()) > 0:
            hi = max(sizes.values())
            short = [k for k, v in sizes.items() if v < hi * 0.75]
            if short:
                out.append((s, f"truncated stem(s): {short}"))
    return out

def cmd_repair(a):
    """Re-split songs that are in the catalogue but have no stems.

    These are entries that were published without ever being split — the app
    lists them and plays silence. They keep their existing catalogue id (so
    favourites and playlists that reference them survive); only the stems and
    the stem URLs in their detail file change.

    The gate still applies: the index's own recorded duration is the reference,
    and a source that does not match it within tolerance is refused rather than
    guessed at.
    """
    import uuid as uuidmod, re as _re
    from ingest_gpu import (resolve_one, produce_one, put_file, head_ok,
                            stem_url, STEMS as ISTEMS, STEMS_LOCAL)
    cat = cl.load_index()
    idx = cl.index_by_id(cat)
    songs = cat["songsList"]
    if a.ids:
        want = {str(x) for x in a.ids}
        songs = [x for x in songs if str(x["id"]) in want]
    if a.force:
        # Explicit re-split: the stems are fine as files but are the WRONG SONG,
        # which no automated check can see. BA$$ served a zouk track under Che's
        # title with four healthy same-length stems and a matching duration.
        if not a.ids:
            sys.exit("  --force needs --ids: it re-splits regardless of any defect")
        bad = [(x, "forced re-split") for x in songs]
    else:
        bad = find_unplayable(songs, check_lengths=a.lengths)
    if a.limit:
        bad = bad[:a.limit]
    log(f"  {len(bad)} unplayable song(s)")
    if not bad:
        return

    def secs(d):
        m = _re.match(r"^(\d+):(\d{2})$", str(d or ""))
        return int(m.group(1)) * 60 + int(m.group(2)) if m else None

    jobs = []
    for s, why in bad:
        dur = secs(s.get("duration"))
        if not dur:
            log(f"    skip {s['id']}: no duration recorded, the gate has no reference")
            continue
        jobs.append({"catalog_id": str(s["id"]), "uuid": str(uuidmod.uuid4()),
                     "title": s.get("title", ""), "artist": s.get("artist", ""),
                     "duration_ms": dur * 1000, "bpm": s.get("bpm"), "key": s.get("key"),
                     "status": "pending", "error": "", "source": None})
    log(f"  {len(jobs)} have a duration to verify against")
    if not a.apply:
        log("  (dry run — pass --apply)"); return

    for i, t in enumerate(jobs, 1):
        resolve_one(t)
        mark = "✓" if t["status"] == "resolved" else "✗"
        log(f"    {mark} [{i}/{len(jobs)}] {t['artist'][:22]:22s} {t['title'][:26]:26s} "
            f"{t.get('error','')[:40]}")
    ok = [t for t in jobs if t["status"] == "resolved"]
    log(f"  resolved {len(ok)}/{len(jobs)}")

    done = []
    for i, t in enumerate(ok, 1):
        log(f"    ▶ [{i}/{len(ok)}] {t['artist'][:22]} — {t['title'][:26]}")
        produce_one(t)
        if t["status"] != "produced":
            log(f"      ✗ {t.get('error','')[:70]}"); continue
        local = STEMS_LOCAL / t["catalog_id"]
        fails = []
        for st in ISTEMS:
            okk, err = put_file(local / f"{st}.m4a", f"catalogtracks/{t['uuid']}/{st}.m4a")
            if not okk:
                fails.append(f"{st}:{err}")
        if fails:
            log(f"      ✗ upload {fails}"); continue
        if not all(head_ok(stem_url(t, st)) for st in ISTEMS):
            log("      ✗ uploaded but not serving"); continue
        done.append(t)
        log("      ✓ 4/4 stems live")

    if not done:
        log("  nothing repaired."); return
    files, ids = [], []
    for t in done:
        doc = cl.load_song(t["catalog_id"]) or {"id": t["catalog_id"],
                                                "title": t["title"], "artist": t["artist"]}
        doc["stems"] = [{"id": st, "name": st.upper(), "url": stem_url(t, st)}
                        for st in ISTEMS]
        if t.get("analysis"):
            doc.setdefault("bpm", t["analysis"].get("bpm"))
            doc.setdefault("key", t["analysis"].get("key"))
        files.append(cl.write_song(t["catalog_id"], doc))
        ids.append(t["catalog_id"])
    cl.publish(files, ids)
    log(f"  repaired {len(done)} song(s); {len(jobs)-len(done)} still unplayable")

# ── dedupe ──────────────────────────────────────────────────────────────────
def cmd_dedupe(a):
    """Resolve songs that collide under the app's own dedup rule.

    The app drops later songs sharing (title, lead artist) at LOAD time, so a
    collision means one of the pair is invisible in the library — you paid to
    split it and cannot reach it. Worse, the survivor is simply whichever comes
    first in the index, which is not necessarily the working one: 'Chromer'
    showed a copy with no stems while a complete copy sat hidden behind it.

    Rule: keep the copy the app already shows, UNLESS it is unplayable — then
    keep the playable one instead. The loser is removed from the index only;
    its JSON and stems stay on the CDN.
    """
    cat = cl.load_index()
    songs = cat["songsList"]
    order = {str(s["id"]): i for i, s in enumerate(songs)}
    groups = defaultdict(list)
    for s in songs:
        groups[(re.sub(r"[^a-z0-9]", "", s.get("title", "").lower()),
                primary_artist(s.get("artist", "")))].append(s)
    dupes = [v for v in groups.values() if len(v) > 1]
    if not dupes:
        log("  no collisions."); return
    log(f"  {len(dupes)} colliding group(s)")

    plans = []
    for grp in dupes:
        grp = sorted(grp, key=lambda s: order[str(s["id"])])
        playable = {}
        for s in grp:
            doc = cl.load_song(s["id"])
            urls = cl.stem_urls(doc) if doc else {}
            playable[str(s["id"])] = bool(urls) and all(cl.head_ok(u) for u in urls.values())
        shown = grp[0]
        keep = shown if playable.get(str(shown["id"])) else next(
            (s for s in grp if playable.get(str(s["id"]))), shown)
        drop = [s for s in grp if str(s["id"]) != str(keep["id"])]
        # carry the ';' collaborator convention onto the survivor if a loser has it
        # Adopt the ';' collaborator formatting ONLY when it credits the same
        # people. "Tiakola x Genezio x Prototype" -> "Tiakola; Genezio; Prototype"
        # is a pure reformat and welcome; "La Rvfleuze" -> "La Rvfleuze; COLORS"
        # would invent a collaborator out of a session brand.
        better = next((s for s in drop
                       if ";" in (s.get("artist") or "")
                       and ";" not in (keep.get("artist") or "")
                       and credited(s.get("artist")) == credited(keep.get("artist"))), None)
        plans.append((keep, drop, better, playable))
        why = "" if playable.get(str(shown["id"])) else "  (the shown copy is UNPLAYABLE)"
        log(f"    keep {keep['id']:>5} {keep.get('artist','')[:26]:26s} {keep.get('title','')[:22]:22s}{why}")
        for s in drop:
            log(f"    drop {s['id']:>5} {s.get('artist','')[:26]:26s} "
                f"{'playable' if playable.get(str(s['id'])) else 'UNPLAYABLE'}")
        if better:
            log(f"         + adopt artist {better['artist'][:44]!r}")
    if not a.apply:
        log("\n  (dry run — pass --apply)"); return

    files, ids = [], []
    for keep, drop, better, _pl in plans:
        if better:
            files += cl.relabel(keep["id"], artist=better["artist"])
            ids.append(str(keep["id"]))
        for s in drop:
            cl.remove_from_index(s["id"])
    cl.publish(files, ids)
    log(f"  removed {sum(len(d) for _k, d, _b, _p in plans)} duplicate entr(ies)")

# ── relabel ─────────────────────────────────────────────────────────────────
def cmd_relabel(a):
    kw = {k: v for k, v in dict(
        title=a.title, artist=a.artist, cover=a.cover, year=a.year, album=a.album,
        bpm=a.bpm, key=a.key, producers=a.producers, writers=a.writers,
        label=a.label, duration=a.duration, drop_fallback=a.drop_fallback).items()
        if v not in (None, False)}
    if not kw:
        sys.exit("  nothing to change")
    cat = cl.load_index()
    cur = cl.index_by_id(cat).get(str(a.id))
    if not cur:
        sys.exit(f"  id {a.id} not in the index")
    log(f"  before: {cur.get('artist')} — {cur.get('title')}")
    files = cl.relabel(a.id, **kw)
    cat2 = cl.load_index()
    new = cl.index_by_id(cat2).get(str(a.id))
    log(f"  after : {new.get('artist')} — {new.get('title')}")
    if a.apply:
        cl.publish(files, [str(a.id)])
    else:
        log("  (written locally; pass --apply to publish)")

# ── verify ──────────────────────────────────────────────────────────────────
def cmd_verify(a):
    cat = cl.load_index()
    idx = cl.index_by_id(cat)
    ids = a.ids or list(idx)[-a.last:] if a.last else (a.ids or [])
    if not ids:
        sys.exit("  pass --ids or --last N")
    bad = 0
    for cid in ids:
        s = idx.get(str(cid))
        if not s:
            log(f"  {cid}: not in index"); bad += 1; continue
        doc = cl.load_song(cid)
        if not doc:
            log(f"  {cid}: no detail JSON"); bad += 1; continue
        urls = cl.stem_urls(doc)
        live = sum(1 for u in urls.values() if cl.head_ok(u))
        mark = "OK" if live == 4 else "!!"
        if live != 4:
            bad += 1
        log(f"  [{mark}] {cid:>5} {s.get('artist','')[:22]:22s} {s.get('title','')[:26]:26s} stems {live}/4")
    log(f"\n  {len(ids)-bad}/{len(ids)} fully playable")

def main():
    ap = argparse.ArgumentParser(description="Zingy catalogue tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add"); p.add_argument("--csv")
    p.add_argument("--album", action="append", help='"Artist/Album Name"')
    p.add_argument("--track", action="append", help='"Artist - Title"')
    p.add_argument("--line", choices=["gpu", "replicate"], default="gpu",
                   help="where the stems are made: the home GPU box, or Replicate")

    p = sub.add_parser("audit"); p.add_argument("--deep", type=int, default=0)

    p = sub.add_parser("fix"); p.add_argument("what", choices=["romaji", "artists", "covers", "producers", "bpmkey", "vibe", "lyrics", "lyrics-version", "index-credits"])
    p.add_argument("--apply", action="store_true"); p.add_argument("--limit", type=int, default=0)

    p = sub.add_parser("dedupe"); p.add_argument("--apply", action="store_true")

    p = sub.add_parser("repair"); p.add_argument("--apply", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--lengths", action="store_true",
                   help="also re-split songs whose stems differ wildly in length")
    p.add_argument("--ids", nargs="*", help="restrict to these catalogue ids")
    p.add_argument("--force", action="store_true",
                   help="re-split the given --ids even if nothing looks broken "
                        "(for stems that are healthy but the wrong song)")

    p = sub.add_parser("relabel"); p.add_argument("id")
    for f in ("title", "artist", "cover", "year", "album", "key", "producers", "writers", "label"):
        p.add_argument(f"--{f}")
    p.add_argument("--bpm", type=int); p.add_argument("--duration", type=int)
    p.add_argument("--drop-fallback", dest="drop_fallback", action="store_true")
    p.add_argument("--apply", action="store_true")

    p = sub.add_parser("verify"); p.add_argument("--ids", nargs="*")
    p.add_argument("--last", type=int, default=0)

    sub.add_parser("status")

    p = sub.add_parser("spotify"); p.add_argument("action", choices=["login", "use", "pull", "status", "schedule"])
    p.add_argument("arg", nargs="?"); p.add_argument("--apply", action="store_true")

    a = ap.parse_args()
    {"add": cmd_add, "audit": cmd_audit, "fix": cmd_fix, "relabel": cmd_relabel, "spotify": cmd_spotify,
     "dedupe": cmd_dedupe, "repair": cmd_repair,
     "verify": cmd_verify, "status": lambda _a: run(["status"])}[a.cmd](a)

if __name__ == "__main__":
    main()
