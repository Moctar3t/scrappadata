#!/usr/bin/env python3
"""
auto_ingest.py — ONE command, whole catalogue pipeline.

    ZINGY_WORKER_SECRET=... python3 auto_ingest.py "<youtube playlist/video url | exportify.csv>"

Chains every stage and prevents most duplicates, making playlist re-runs idempotent:

  1. GATE        ingest_playlist.py — verified source (Topic/master > video, covers
                 rejected), authoritative identity.
  2. DEDUP       drop anything whose (title, primary-artist) is already in the
                 catalogue (normalised, feat-stripped). The enqueue endpoint also
                 dedups by youtubeUrl, so re-running a playlist is a no-op.
  3. ENQUEUE     POST the NEW tracks to the VPS split queue.
  4. WAIT        poll until the home GPU worker has separated them (status=ready).
  5. ENRICH      local_metadata_enricher.py (Genius producers/feat/year + Deezer
                 label) → polish_catalog.py (clean titles + backfill cover art).
  6. EXPORT      export_catalog.py (DB → musicdata JSON, owner-filtered, id-matched,
                 featuring merged into artist — no dupes, no junk).
  7. PUBLISH     scripts/upload-catalog.sh (mirror to BunnyCDN + edge purge).

The app then hot-reloads the new catalogue from the CDN. No app rebuild needed.
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path("/Users/dime/Developer/ios-zingy")
VPS = "204.168.236.140"
API = "https://zingy.dime.systems/api"
PY = sys.executable


def run(cmd, **kw):
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True, **kw)


def get_secret() -> str:
    """ZINGY_WORKER_SECRET from the env, else straight from the VPS .env."""
    s = os.environ.get("ZINGY_WORKER_SECRET")
    if s:
        return s
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=12", f"root@{VPS}",
                        "grep '^WORKER_SECRET=' /var/www/zingy-api/.env | cut -d= -f2"],
                       capture_output=True, text=True)
    s = r.stdout.strip()
    if not s:
        sys.exit("couldn't read WORKER_SECRET from the VPS — set ZINGY_WORKER_SECRET manually")
    print("[auto] worker secret pulled from the VPS")
    return s


def box(cmd: str, timeout: int = 90) -> str:
    """Run a command on the GPU box over Tailscale. Always bounded — box.exp used
    to carry `set timeout -1`, so a half-open SSH session could hang the whole
    pipeline with no output and no way to tell it apart from a slow split."""
    exp = HERE / "box.exp"
    if not exp.exists():
        return ""
    try:
        r = subprocess.run(["expect", str(exp), cmd],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"
    except Exception as e:
        return f"__ERROR__ {e}"


def worker_heartbeat() -> dict | None:
    """The worker rewrites heartbeat.json on every poll. 'schtasks says Running'
    is not the same as 'the worker is alive' — a wedged python process keeps the
    task green forever. The heartbeat is what actually distinguishes them."""
    out = box(r'powershell -Command "Get-Content $env:USERPROFILE\demucs_relay\heartbeat.json -EA SilentlyContinue"')
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def ensure_worker(verbose: bool = True) -> bool:
    """Make sure the GPU box's ZingyWorker is actually running AND polling.
    A worker pointed at a dead API, or simply not started, looks EXACTLY like a
    hung pipeline — this is the failure that wasted a whole night in 2026-06."""
    exp = HERE / "box.exp"
    if not exp.exists():
        print("[auto] box.exp missing — can't check the GPU worker, make sure it runs")
        return False

    hb = worker_heartbeat()
    if hb and abs(time.time() - hb.get("epoch", 0)) < 120:
        # abs(): the Mac and the Windows box do not agree on the second, so a
        # fresh heartbeat can read as slightly in the future.
        age = max(0, int(time.time() - hb["epoch"]))
        if verbose:
            print(f"[auto] GPU worker: alive ✓ (state={hb.get('state')}, {age}s ago)")
        return True

    out = box("schtasks /Query /TN ZingyWorker /FO CSV /NH")
    if out == "__TIMEOUT__":
        print("[auto] ✗ GPU box did not answer in 90s — Tailscale down, or the box asleep.")
        print("       try: tailscale status   |   ./wake_gpu.sh")
        return False
    running = "cours" in out or "Running" in out         # « En cours d'exécution »

    if running and hb is None:
        # Task green but no heartbeat: either an old worker build (no heartbeat
        # support) or a wedged process. Restart is right either way.
        print("[auto] GPU worker task is green but not heartbeating — restarting it…")
    elif not running:
        print("[auto] GPU worker not running — starting it…")

    box("schtasks /End /TN ZingyWorker", timeout=60)
    box("schtasks /Change /TN ZingyWorker /ENABLE & schtasks /Run /TN ZingyWorker", timeout=60)

    for _ in range(10):
        time.sleep(6)
        hb = worker_heartbeat()
        if hb and time.time() - hb.get("epoch", 0) < 120:
            print(f"[auto] GPU worker: started ✓ (state={hb.get('state')})")
            return True
    print("[auto] ⚠️  worker started but never heartbeat — check worker.log on the box")
    return False


def preflight(need_gpu: bool = True) -> bool:
    """Every precondition, checked in seconds, BEFORE a 40-minute wait can begin.
    The old script discovered a dead worker only after enqueuing and polling for
    ten minutes; every failure below used to present as an unexplained hang."""
    print("── preflight ────────────────────────────────────────────────")
    ok = True

    r = subprocess.run(["ssh", "-o", "ConnectTimeout=10", f"root@{VPS}", "true"],
                       capture_output=True, text=True)
    print(f"  VPS ssh          : {'✓' if r.returncode == 0 else '✗'}")
    ok &= r.returncode == 0

    try:
        with urllib.request.urlopen(API + "/health", timeout=10) as h:
            print(f"  API {API:<12} : {'✓' if h.status == 200 else '✗ ' + str(h.status)}")
    except Exception as e:
        print(f"  API {API:<12} : ✗ {e}")
        ok = False

    if need_gpu:
        ts = subprocess.run(["/usr/local/bin/tailscale", "status"],
                            capture_output=True, text=True)
        up = "technica" in ts.stdout
        print(f"  Tailscale/technica: {'✓' if up else '✗ box not in the tailnet'}")
        ok &= up
        if up:
            ok &= ensure_worker()
            # File age says nothing: yt-dlp REWRITES cookies.txt every time it is
            # passed with --cookies, so a jar exported months ago reads as "today".
            # What matters is whether it carries LOGIN_INFO scoped to .youtube.com
            # — the cookie YouTube uses to prove sign-in. Without it, age-gated
            # videos fail on every player client, which is what put 6 songs in
            # 'stuck'. The worker computes this on startup; read its verdict.
            tail = box(r'powershell -Command "Select-String -Path $env:USERPROFILE\demucs_relay\worker.log -Pattern \"zingy-gpu-worker. cookies\" | Select-Object -Last 1"', timeout=90)
            if "OK " in tail:
                print("  YouTube cookies  : ✓ signed in to youtube.com")
            elif "BAD" in tail:
                print("  YouTube cookies  : ✗ jar is LOGGED OUT — age-restricted "
                      "videos will fail")
                print("                     re-export cookies.txt from a browser tab "
                      "signed in on youtube.com")
            else:
                print("  YouTube cookies  : ? worker has not reported yet")

    bad = broken_rows()
    if bad:
        print(f"  broken rows      : ⚠ {len(bad)} catalogue song(s) have no stems "
              f"(run with --repair to requeue them)")
    else:
        print("  broken rows      : ✓ none")

    print("─────────────────────────────────────────────────────────────")
    return ok


def psql(q: str) -> str:
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=12", f"root@{VPS}",
                        f"sudo -u postgres psql -d zingy -tA -c \"{q}\""],
                       capture_output=True, text=True)
    return r.stdout.strip()


def norm(s: str) -> str:
    s = re.sub(r'\(feat[^)]*\)|\bfeat\b.*|\bft\b.*', '', (s or '').lower())
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


def sanitize_youtube_url(url: str) -> str:
    url = url.strip()
    if not (url.startswith(("http://", "https://", "www.")) or "youtube.com" in url or "youtu.be" in url):
        return url
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)

    vid = None
    if "v" in qs:
        vid = qs["v"][0]
    elif parsed.netloc == "youtu.be" or parsed.netloc.endswith(".youtu.be"):
        vid = parsed.path.strip("/")

    playlist_id = None
    if "list" in qs:
        playlist_id = qs["list"][0]

    is_mix_or_radio = False
    if playlist_id and playlist_id.startswith("RD"):
        is_mix_or_radio = True
    if "start_radio" in qs:
        is_mix_or_radio = True

    if is_mix_or_radio:
        if vid:
            print(f"[auto] Sanity: mix/radio link detected. Resolving to seed video: https://www.youtube.com/watch?v={vid}")
            return f"https://www.youtube.com/watch?v={vid}"
        elif playlist_id:
            return f"https://www.youtube.com/playlist?list={playlist_id}"

    if playlist_id:
        return f"https://www.youtube.com/playlist?list={playlist_id}"
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    return url


def catalog_keys() -> tuple[set, list]:
    """Dedup index over BOTH catalogues: the VPS tracks table AND the app's
    music-index.json (2.8K songs incl. LEGACY ones that were never in the DB —
    without this, anything already in the app but pre-DB would re-ingest).

    A row only counts as "already in the catalogue" when it ACTUALLY HAS STEMS.
    This used to select every __catalog__ row regardless of status, so a track
    that was enqueued and then failed to split was remembered as present forever:
    re-running the exact same URL printed "[dup] already in catalogue" and the
    song could never be recovered. 16 rows are in that state right now — 15
    marked 'ready' with four NULL stem URLs plus one 'stuck' — and they shipped
    into the app as songs that play nothing.

    Returns (exact keys, [(cat_title_norm, artist_tokens, duration_s)] for near-dup checks)."""
    keys, near = set(), []

    def add(title, artist, dur_s):
        keys.add((norm(title), norm((artist or "").split(";")[0])))
        toks = {norm(p) for p in re.split(r"[;,&]|feat\.?|ft\.?", (artist or "").lower()) if norm(p)}
        if toks and dur_s:
            near.append((norm(title), toks, dur_s))

    rows = psql("SELECT row_to_json(r) FROM (SELECT title,artist,duration_sec FROM tracks "
                "WHERE owner_id=(SELECT id FROM users WHERE username='__catalog__') "
                "AND status='ready' AND bass_url IS NOT NULL AND drums_url IS NOT NULL "
                "AND vocals_url IS NOT NULL AND other_url IS NOT NULL) r")
    for l in rows.splitlines():
        try:
            t = json.loads(l)
            add(t["title"], t.get("artist"), t.get("duration_sec") or 0)
        except Exception:
            pass

    idx = REPO / "App/ZingyApp/Resources/musicdata/music-index.json"
    songs_dir = REPO / "App/ZingyApp/Resources/musicdata/songs"
    try:
        for s in json.load(open(idx, encoding="utf-8")).get("songsList", []):
            # Same rule for the legacy app catalogue: an index entry whose song
            # file has fewer than 4 stems is not a song, it is a hole.
            sf = songs_dir / f"{int(s.get('id', 0)):04d}.json"
            try:
                if len(json.load(open(sf, encoding="utf-8")).get("stems") or []) < 4:
                    continue
            except Exception:
                pass
            d = s.get("duration") or "0:00"
            try:
                mm, ss = str(d).split(":"); dur = int(mm) * 60 + int(ss)
            except Exception:
                dur = 0
            add(s.get("title", ""), s.get("artist", ""), dur)
    except Exception as e:
        print(f"[auto] WARNING: couldn't read the app index for dedup ({e})")
    return keys, near


def broken_rows() -> list[dict]:
    """Catalogue rows that claim to exist but have no playable stems."""
    out = psql("SELECT row_to_json(r) FROM (SELECT id,title,artist,status,duration_sec,"
               "(original_url IS NOT NULL) AS has_url, original_url FROM tracks "
               "WHERE owner_id=(SELECT id FROM users WHERE username='__catalog__') "
               "AND (status<>'ready' OR bass_url IS NULL OR drums_url IS NULL "
               "OR vocals_url IS NULL OR other_url IS NULL) ORDER BY created_at DESC) r")
    rows = []
    for l in out.splitlines():
        try:
            rows.append(json.loads(l))
        except Exception:
            pass
    return rows


def resolve_orphan(r: dict) -> str | None:
    """An orphan row has no original_url, so requeuing it does nothing — the
    worker would claim a job with no source. Re-derive the source through the
    SAME verified gate that ingest_playlist uses (Topic/master preferred, covers
    and live versions rejected, duration must match), never a blind ytsearch1.

    Run as a SUBPROCESS with a hard wall-clock kill. A direct in-process call
    hung for 19+ minutes on a dead TLS socket (CLOSE_WAIT to a Google IP, zero
    bytes exchanged) — ytmusicapi's documented 30s request timeout does not
    cover whatever layer that hang was in, and nothing short of killing the
    process got it back. A subprocess can be killed outright; the caller can't
    be trusted to un-hang itself."""
    if not r.get("title") or not r.get("artist") or not r.get("duration_sec"):
        print("       cannot resolve — row has no title/artist/duration to match on")
        return None
    script = (
        "import sys, json, ingest_playlist as gate\n"
        "from ytmusicapi import YTMusic\n"
        "row = json.loads(sys.argv[1])\n"
        "t = gate.Track(spotify_id='', title=row['title'],\n"
        "    artists=[a.strip() for a in row['artist'].split(';') if a.strip()],\n"
        "    album='', duration_ms=int(row['duration_sec'] * 1000))\n"
        "gate.pick(t, gate.search_candidates(t, 8, YTMusic()))\n"
        "print('RESULT:' + json.dumps({'status': t.status, 'url': t.youtube_url,\n"
        "    'reasons': t.reasons[:2]}))\n"
    )
    try:
        p = subprocess.run([PY, "-c", script, json.dumps(r)],
                           capture_output=True, text=True, timeout=90, cwd=str(HERE))
    except subprocess.TimeoutExpired:
        print("       cannot resolve — gate subprocess did not answer in 90s (killed)")
        return None
    line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT:")), None)
    if not line:
        print(f"       cannot resolve — gate subprocess failed: {(p.stderr or '').strip()[-200:]}")
        return None
    res = json.loads(line[len("RESULT:"):])
    if res["status"] != "accepted" or not res["url"]:
        print(f"       gate says {res['status']}: {'; '.join(res['reasons'])}")
        return None
    return res["url"]


def requeue_broken(resolve: bool = True) -> int:
    """Put stemless / stuck catalogue rows back on the GPU queue. This is the
    repair for anything the old dedup had frozen out."""
    rows = broken_rows()
    if not rows:
        print("[auto] no broken catalogue rows — nothing to requeue")
        return 0

    have_url = [r for r in rows if r.get("has_url")]
    orphans = [r for r in rows if not r.get("has_url")]
    print(f"[auto] {len(rows)} catalogue row(s) exist without playable stems "
          f"({len(have_url)} requeueable, {len(orphans)} with no source URL)")

    fixed = 0
    for r in orphans:
        print(f"  [orphan] {r['id'][:8]}  {r.get('artist','?')} — {r.get('title','?')}")
        if not resolve:
            continue
        url = resolve_orphan(r)
        if url:
            safe = url.replace("'", "''")
            psql(f"UPDATE tracks SET original_url='{safe}' WHERE id='{r['id']}'::uuid")
            print(f"       ✓ resolved -> {url}")
            fixed += 1

    ids = [r["id"] for r in have_url] + [r["id"] for r in orphans]
    inlist = ",".join(f"'{i}'::uuid" for i in ids)
    psql("UPDATE tracks SET status='queued', claimed_by=NULL, claimed_at=NULL, error=NULL "
         f"WHERE id IN ({inlist}) AND original_url IS NOT NULL")
    n = len(have_url) + fixed
    print(f"[auto] requeued {n} track(s) for the GPU worker"
          + (f"; {len(orphans)-fixed} still unresolvable" if len(orphans) - fixed else ""))
    return n


def probable_dup(t: dict, near: list) -> bool:
    """Cross-script duplicate net: same artist + same duration (±2s) AND titles
    share at least one word (or one contains the other) to prevent collisions."""
    if not t.get("duration_ms"):
        return False
    dur = t["duration_ms"] / 1000
    toks = {norm(a) for a in (t.get("artists") or []) if norm(a)}
    t_title = norm(t.get("title") or "")
    for cat_title, cat_toks, cat_dur in near:
        if abs(cat_dur - dur) <= 2 and any(
                a and c and (a in c or c in a) for a in toks for c in cat_toks):
            if t_title and cat_title:
                if t_title in cat_title or cat_title in t_title:
                    return True
                w1 = {w for w in t_title.split() if len(w) >= 3}
                w2 = {w for w in cat_title.split() if len(w) >= 3}
                if not w1.isdisjoint(w2):
                    return True
    return False


def enqueue(track: dict, secret: str) -> str | None:
    # Full payload — CSV/gate data is authoritative and rides in up front
    # (year, credits, label, cover pair), so the enricher only fills true gaps.
    payload = {
        "youtubeUrl": track["youtube_url"],
        "title": track["title"],
        "artist": "; ".join(track["artists"]) if track.get("artists") else None,
        "bpm": track.get("bpm"),
        "songKey": track.get("musical_key"),
        "durationSec": round(track["duration_ms"] / 1000) if track.get("duration_ms") else None,
        "coverUrl": track.get("art_url"),
        "coverUrlFallback": track.get("art_url_fallback"),
        "year": track.get("year"),
        "producers": "; ".join(track["producers"]) if track.get("producers") else None,
        "featuring": "; ".join(track["featuring"]) if track.get("featuring") else None,
        "writers": "; ".join(track["writers"]) if track.get("writers") else None,
        "releaseDate": track.get("release_date") or None,
        "recordLabel": track.get("label") or None,
    }
    req = urllib.request.Request(API + "/internal/split/catalog",
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "x-worker-secret": secret})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("id")
    except Exception as e:
        print(f"  enqueue failed for {track['title']}: {e}")
        return None


def wait_ready(ids: list, timeout_s: int | None = None) -> None:
    if not ids:
        return
    # Scale with the batch: a flat 40 min was fine for a playlist but silently
    # truncated big CSV drops (1.5K songs ≈ 13h of GPU) — the run then
    # exported/published only the part that had finished by the deadline.
    if timeout_s is None:
        timeout_s = max(2400, 120 * len(ids))
    inlist = ",".join(f"'{i}'" for i in ids)
    start = time.time()
    stalled = 0
    while time.time() - start < timeout_s:
        out = psql(f"SELECT status, count(*) FROM tracks WHERE id IN ({inlist}) GROUP BY status")
        counts = {}
        for l in out.splitlines():
            if "|" in l:
                s, c = l.split("|"); counts[s] = int(c)
        done = counts.get("ready", 0) + counts.get("failed", 0)
        print(f"  [wait] ready={counts.get('ready',0)} failed={counts.get('failed',0)} "
              f"processing={counts.get('processing',0)} queued={counts.get('queued',0)} "
              f"({int(time.time()-start)}s)")
        if done >= len(ids):
            return
        # Stall check: songs queued, nothing processing → the worker isn't
        # claiming. Instead of printing instructions and hoping, pull the actual
        # worker log here and restart it — the old version told you to go read a
        # file on a Windows box mid-run, which nobody does.
        if counts.get("queued", 0) > 0 and counts.get("processing", 0) == 0:
            stalled += 1
            if stalled == 4:
                print("  [wait] ⚠️  nothing is being claimed — inspecting the GPU worker…")
                tail = box(r'powershell -Command "Get-Content $env:USERPROFILE\demucs_relay\worker.log -Tail 12 -EA SilentlyContinue"', timeout=90)
                for l in tail.splitlines()[-12:]:
                    if l.strip():
                        print(f"         | {l.rstrip()}")
                ensure_worker()
                stalled = 0
        else:
            stalled = 0
        time.sleep(15)
    print("  [wait] timed out — proceeding with whatever finished")


def wait_for_repair(expected: int, timeout_s: int = 3600) -> None:
    """Poll until the requeued rows come back with real stems (or stop moving)."""
    start = time.time()
    while time.time() - start < timeout_s:
        left = len(broken_rows())
        done = expected - left
        print(f"  [repair] {done}/{expected} recovered, {left} still without stems "
              f"({int(time.time()-start)}s)")
        if left == 0:
            print("  [repair] ✓ all recovered")
            return
        time.sleep(20)
    print("  [repair] timed out — remaining rows are listed by --check")


def merge_csv_extras(pairs: list) -> None:
    """Write genres + audio features + popularity from the gate manifest into the
    app index entries created by export_catalog (matched via catalog_id_map)."""
    feats6 = ("danceability", "energy", "acousticness", "instrumentalness",
              "valence", "speechiness")
    try:
        id_map = json.load(open(HERE / "catalog_id_map.json", encoding="utf-8"))
    except Exception:
        print("[auto] no catalog_id_map.json — skipping CSV-extras merge")
        return
    idx_path = REPO / "App/ZingyApp/Resources/musicdata/music-index.json"
    catalog = json.load(open(idx_path, encoding="utf-8"))
    by_id = {str(s.get("id")): s for s in catalog.get("songsList", [])}
    merged = 0
    for t, uuid in pairs:
        s = by_id.get(str(id_map.get(uuid, "")))
        if not s:
            continue
        genres = [g.strip() for g in (t.get("genres") or "").split(",") if g.strip()]
        if genres and not s.get("genres"):
            s["genres"] = genres
        f = t.get("features") or {}
        for k in feats6:
            if k in f and s.get(k) is None:
                s[k] = f[k]
        if t.get("popularity") and not s.get("popularity"):
            s["popularity"] = t["popularity"]
        merged += 1
    json.dump(catalog, open(idx_path, "w", encoding="utf-8"),
              separators=(",", ":"), ensure_ascii=False)
    print(f"[auto] merged CSV genres/features into {merged} index entr(ies)")


def final_report(ids: list) -> None:
    """The 'is everything actually right' sweep: metadata coverage per new song
    + a real HTTP check on all 4 stems on the CDN."""
    if not ids:
        return
    inlist = ",".join(f"'{i}'" for i in ids)
    out = psql("SELECT row_to_json(r) FROM (SELECT id,title,artist,producers,writers,year,"
               f"record_label,bpm,song_key,cover_url,cover_url_fallback,bass_url,drums_url,"
               f"vocals_url,other_url,status,error FROM tracks WHERE id IN ({inlist})) r")
    rows = [json.loads(l) for l in out.splitlines() if l.strip()]
    print("\n── FINAL SWEEP ─────────────────────────────────────────────")
    problems = 0
    for t in rows:
        marks = []
        for f, lbl in (("producers", "prod"), ("writers", "writ"), ("year", "year"),
                       ("record_label", "label"), ("bpm", "bpm"), ("song_key", "key"),
                       ("cover_url", "cover")):
            marks.append(("✓" if t.get(f) else "✗") + lbl)
        stem_ok = 0
        for k in ("bass_url", "drums_url", "vocals_url", "other_url"):
            u = t.get(k)
            if u:
                try:
                    req = urllib.request.Request(u, method="HEAD")
                    with urllib.request.urlopen(req, timeout=15) as r:
                        stem_ok += 1 if r.status == 200 else 0
                except Exception:
                    pass
        stems = f"stems {stem_ok}/4"
        status = t.get("status")
        line_ok = status == "ready" and stem_ok == 4
        if not line_ok or "✗" in "".join(marks):
            problems += 1
        print(f"  [{'OK' if line_ok else '!!'}] {t.get('artist','?')} — {t.get('title','?')}"
              f"  ({status}, {stems})  {' '.join(marks)}"
              + (f"  error={t['error'][:60]}" if t.get("error") else ""))
    print(f"── {len(rows)-problems}/{len(rows)} fully clean "
          f"({problems} with gaps — usually obscure songs the sources don't know)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", help="YouTube playlist/video URL or Exportify CSV path")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--skip-publish", action="store_true")
    ap.add_argument("--force", action="store_true", help="Force ingestion, bypassing duplicate checks")
    ap.add_argument("--check", action="store_true",
                    help="run preflight only — is the whole chain healthy?")
    ap.add_argument("--repair", action="store_true",
                    help="requeue catalogue songs that exist but have no stems")
    a = ap.parse_args()

    if a.check:
        sys.exit(0 if preflight() else 1)
    if a.repair:
        preflight()
        n = requeue_broken()
        if n:
            print("[auto] waiting for the GPU to separate the repaired tracks…")
            wait_for_repair(n)
        sys.exit(0)
    if not a.source:
        ap.error("a source URL/CSV is required (or use --check / --repair)")

    if not preflight():
        print("[auto] ✗ preflight failed — fix the above before ingesting "
              "(nothing was enqueued)")
        sys.exit(1)
    secret = get_secret()

    # Pre-process / Sanitize URL
    source = sanitize_youtube_url(a.source)

    # 1. GATE
    cmd = [PY, str(HERE / "ingest_playlist.py"), source, "--out-dir", "/tmp"]
    if a.limit:
        cmd += ["--limit", str(a.limit)]
    run(cmd)
    manifest = json.load(open("/tmp/ingest_manifest.json"))
    accepted = [
        t for t in manifest["tracks"]
        if (t["status"] == "accepted" or (a.force and t["status"] == "manual"))
        and t.get("youtube_url")
    ]
    print(f"\n[auto] gate accepted {len(accepted)} track(s)")

    # 2. DEDUP against BOTH catalogues (DB + app index) and within the batch,
    #    plus the cross-script near-dup net (same artist, same duration ±2s).
    have, near = catalog_keys()
    new = []
    seen = set()
    for t in accepted:
        key = (norm(t["title"]), norm(t["artists"][0] if t.get("artists") else ""))
        if key in have or key in seen:
            print(f"  [dup] already in catalogue: {t.get('artists',['?'])[0]} — {t['title']}")
            continue
        if not a.force and probable_dup(t, near):
            print(f"  [dup?] PROBABLE duplicate (same artist + duration ±2s), skipped: "
                  f"{t.get('artists',['?'])[0]} — {t['title']}  "
                  f"(run with --force if it's genuinely a different song)")
            continue
        seen.add(key)
        new.append(t)
    print(f"[auto] {len(new)} new · {len(accepted)-len(new)} skipped as duplicates")
    if not new:
        print("[auto] nothing new to ingest — done.")
        return

    # 3. ENQUEUE — keep the track↔uuid pairing for the post-export index merge.
    pairs = [(t, i) for t in new if (i := enqueue(t, secret))]
    ids = [i for _, i in pairs]
    print(f"[auto] enqueued {len(ids)} track(s) to the GPU queue")

    # 4. WAIT for the GPU worker (after making sure it's actually running)
    ensure_worker()
    print("[auto] waiting for the home GPU to separate stems…")
    wait_ready(ids)

    # 5. ENRICH + polish — scoped to THIS batch's ids so it can't collide with
    #    (or redo) a catalogue-wide backfill running in parallel.
    ids_file = "/tmp/ingest_ids.txt"
    Path(ids_file).write_text("\n".join(ids))
    run([PY, str(HERE / "local_metadata_enricher.py"), "--ids-file", ids_file])
    run([PY, str(HERE / "polish_catalog.py")])

    # 6. EXPORT
    run([PY, str(HERE / "export_catalog.py")])

    # 6b. Merge the CSV-only riches (genres + audio features + popularity) into
    # the app index — the DB has no columns for them, so they'd otherwise be lost
    # and every new song would land with genres:[] and no Mervec features.
    merge_csv_extras(pairs)

    # 7. PUBLISH — performs an incremental sync of only the changed catalog
    # files to the remote CDN to guarantee fast hot-reloads.
    if not a.skip_publish:
        up = str(REPO / "scripts/upload-catalog.sh")
        delta_file = "/tmp/export_changed.txt"
        if os.path.exists(delta_file) and os.path.getsize(delta_file) > 0:
            run(["bash", up, "--files-from", delta_file])
        else:
            print("[auto] delta file empty or missing, skipping upload (no changes).")

    # 8. FINAL SWEEP — prove the metadata + stems are actually right on the CDN.
    final_report(ids)

    print(f"\n✅ done — {len(ids)} new song(s) live. Foreground the app to hot-reload the catalogue.")


if __name__ == "__main__":
    main()
