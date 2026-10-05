#!/usr/bin/env python3
"""Enrich the iOS app's shipped catalog metadata in place.

Operates DIRECTLY on the app JSON (covers BOTH the DB-backed and the ~1180
legacy songs that the DB->export pipeline never touches). Scrapes Genius
(producers, writers, featuring, release_date, album) + Deezer (label,
release_date, album, record_type, cover) and fills ONLY missing fields —
existing good data is never overwritten.

Two-phase + PER-API caching so it is resumable and gentle on Genius (which
429s aggressively):
  * Phase 1  Deezer (parallel, robust) -> deezer_cache.json
  * Phase 2  Genius  (SERIAL + 429 backoff) -> genius_cache.json
  * Phase 3  apply fills to files

Outputs: per-song songs/XXXX.json (style preserved), music-index.json,
release-formats.json, db_sync_updates.sql, enrich_changes.json.
Re-run to retry whatever Genius didn't reach last time.
"""
import json, os, re, sys, time, urllib.request, urllib.parse, urllib.error, glob, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
SONGS = os.path.join(APP, "songs")
IDMAP = "/Users/dime/Developer/scrappadata/catalog_id_map.json"
GCACHE = "/Users/dime/Developer/scrappadata/genius_cache.json"
DCACHE = "/Users/dime/Developer/scrappadata/deezer_cache.json"
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}

ONLY_MISSING_FMT = "--reformat-singles" not in sys.argv
SKIP_GENIUS = "--skip-genius" in sys.argv      # apply from caches; don't hit Genius
SKIP_DEEZER = "--skip-deezer" in sys.argv

def clean_query_params(title, artist):
    clean_artist = (artist or "").split(';')[0].strip()
    for marker in [" feat", " ft", " featuring", " (with", " & ", " and "]:
        idx = clean_artist.lower().find(marker)
        if idx != -1:
            clean_artist = clean_artist[:idx].strip()
    ct = title or ""
    ct = re.sub(r'@[A-Za-z0-9_]+', '', ct)
    ct = re.sub(r'\((feat|ft|with)\.?.*?\)', '', ct, flags=re.IGNORECASE)
    ct = re.sub(r'\[(feat|ft|with)\.?.*?\]', '', ct, flags=re.IGNORECASE)
    ct = re.sub(r'\b(feat|ft|featuring|with)\b\.?.*$', '', ct, flags=re.IGNORECASE)
    ct = re.sub(r'\b(M/V|Official Video|Official Audio|Clip Officiel|Lyric Video|Audio|Remastered|Remaster)\b', '', ct, flags=re.IGNORECASE)
    ct = re.sub(r'\(\d{4}\)', '', ct)
    ct = re.sub(r'\s*-\s*(Radio Mix|Radio Edit|Live|Remaster.*|Mono|Stereo)\s*$', '', ct, flags=re.IGNORECASE)
    ct = ct.split('//')[0].strip()
    ct = re.sub(r'\s+', ' ', ct).strip()
    ct = re.sub(r'[-–—/]+$', '', ct).strip()
    return ct, clean_artist

def norm(t):
    return re.sub(r'[^a-z0-9]', '', (t or '').lower())

def _read_token():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".genius_token")
    if os.environ.get("GENIUS_TOKEN"):
        return os.environ["GENIUS_TOKEN"].strip()
    if os.path.exists(p):
        return open(p).read().strip()
    return None

GENIUS_TOKEN = _read_token()

def fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode('utf-8'))

def fetch_genius(url):
    # Official token-authed API (api.genius.com) — token-based limits, no IP ban.
    return fetch(url, headers={'Authorization': f'Bearer {GENIUS_TOKEN}', 'User-Agent': 'Zingy/1.0'})

def scrape_genius(title, artist):
    ct, ca = clean_query_params(title, artist)
    nt = norm(ct)
    query = f"{ca} {ct}".strip()
    try:
        data = fetch_genius(f'https://api.genius.com/search?q={urllib.parse.quote(query)}')
        hits = data.get('response', {}).get('hits', [])
        song_id = None
        for hit in hits:
            r = hit.get('result', {})
            ht = norm(r.get('title', ''))
            ha = norm(r.get('primary_artist', {}).get('name', ''))
            if nt and ht and (nt in ht or ht in nt) and (not ca or norm(ca)[:5] in ha or ha[:5] in norm(ca) or ha in norm(artist or '')):
                song_id = r.get('id'); break
        if not song_id and hits:
            song_id = hits[0]['result'].get('id')
        if not song_id:
            return {"_nomatch": True}
        song = fetch_genius(f'https://api.genius.com/songs/{song_id}').get('response', {}).get('song', {})
        prod = [p.get('name') for p in song.get('producer_artists', [])]
        prod_roles = {"producer", "co-producer", "additional production", "additional producer", "produced by"}
        write_roles = {"writer", "writers", "written by", "composer", "composers", "songwriter", "lyricist"}
        writers = [w.get('name') for w in song.get('writer_artists', [])]
        for perf in song.get('custom_performances', []):
            lab = perf.get('label', '').strip().lower()
            if lab in prod_roles:
                prod += [a['name'] for a in perf.get('artists', [])]
            if lab in write_roles:
                writers += [a['name'] for a in perf.get('artists', [])]
        def dedup(lst):
            seen, out = set(), []
            for x in lst:
                k = (x or '').strip().lower()
                if k and k not in seen and k not in {"self-produced", "n/a", "none"}:
                    seen.add(k); out.append(x.strip())
            return out
        prod = dedup(prod); writers = dedup(writers)
        feats = [f.get('name') for f in song.get('featured_artists', [])]
        album = (song.get('album') or {}).get('name') if song.get('album') else None
        return {
            "producers": ", ".join(prod) if prod else None,
            "writers": ", ".join(writers) if writers else None,
            "featuring": ", ".join(feats) if feats else None,
            "release_date": song.get('release_date'),
            "album": album,
        }
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP{e.code}"}
    except Exception as e:
        return {"_error": f"{type(e).__name__}"}

def scrape_deezer(title, artist):
    ct, ca = clean_query_params(title, artist)
    try:
        q = f'track:"{ct}" artist:"{ca}"'
        data = fetch(f'https://api.deezer.com/search?q={urllib.parse.quote(q)}&limit=5')
        tracks = data.get('data', [])
        if not tracks:
            data = fetch(f'https://api.deezer.com/search?q={urllib.parse.quote(ca+" "+ct)}&limit=5')
            tracks = data.get('data', [])
        if not tracks:
            return {"_nomatch": True}
        nt = norm(ct)
        best = next((t for t in tracks if nt and (nt in norm(t.get('title', '')) or norm(t.get('title', '')) in nt)), tracks[0])
        album_id = best.get('album', {}).get('id')
        if not album_id:
            return {"_nomatch": True}
        time.sleep(0.05)
        alb = fetch(f'https://api.deezer.com/album/{album_id}')
        # Deezer's own "no label data" placeholder for self-released/independent
        # tracks is a literal "-" — filling that in looks like a broken field, not
        # a label. Treat it the same as absent. (Found via id 111, Buckshot —
        # Fever: Deezer's API genuinely returns label:"-".)
        label = (alb.get('label') or '').strip()
        return {
            "label": label if label and label != "-" else None,
            "release_date": alb.get('release_date'),
            "album": alb.get('title'),
            "release_type": alb.get('record_type'),
            "cover": alb.get('cover_xl') or alb.get('cover_big') or None,
        }
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP{e.code}"}
    except Exception as e:
        return {"_error": f"{type(e).__name__}"}

def needs(song):
    miss = set()
    for f in ["producers", "writers", "year", "recordLabel", "coverArt"]:
        v = song.get(f)
        if v is None or (isinstance(v, str) and not v.strip()) or (f == "year" and str(v).lower() in {"unknown", "0", ""}):
            miss.add(f)
    return miss

def load(p):
    if os.path.exists(p):
        try: return json.load(open(p, encoding="utf-8"))
        except Exception: return {}
    return {}

def usable(entry):
    return isinstance(entry, dict) and "_error" not in entry

def main():
    id_map = json.load(open(IDMAP, encoding="utf-8"))
    num_to_uuid = {str(v): k for k, v in id_map.items()}
    release_formats = json.load(open(os.path.join(APP, "release-formats.json"), encoding="utf-8"))
    index = json.load(open(os.path.join(APP, "music-index.json"), encoding="utf-8"))
    idx_by_id = {str(s.get("id")): s for s in index.get("songsList", [])}

    gcache, dcache = load(GCACHE), load(DCACHE)
    # one-time seed from the old combined cache, if present
    old = load("app_enrich_cache.json")
    for k, v in old.items():
        if isinstance(v, dict):
            if usable(v.get("genius")) and k not in gcache: gcache[k] = v["genius"]
            if usable(v.get("deezer")) and k not in dcache: dcache[k] = v["deezer"]

    files = sorted(glob.glob(os.path.join(SONGS, "*.json")))
    songs, todo = {}, []
    for fp in files:
        raw = open(fp, encoding="utf-8").read()
        s = json.loads(raw)
        sid = str(s.get("id", ""))
        songs[sid] = {"fp": fp, "s": s, "pretty": "\n  " in raw}
        miss = needs(s)
        fmt_needs = sid not in release_formats or (not ONLY_MISSING_FMT and release_formats.get(sid) == "Single")
        if (miss or fmt_needs) and (s.get("title") or "").strip():
            todo.append((sid, miss))

    def ck(s): return f"{norm((s.get('artist') or '').split(';')[0])}|{norm(s.get('title'))}"

    # which songs still need a Genius / Deezer fetch
    g_jobs = [sid for sid, miss in todo if (miss & {"producers", "writers"}) and ck(songs[sid]["s"]) not in gcache]
    d_jobs = [sid for sid, miss in todo if (miss & {"recordLabel", "coverArt", "year"} or sid not in release_formats) and ck(songs[sid]["s"]) not in dcache]
    # dedup by cache key (multiple songs can share an artist|title)
    print(f"{len(songs)} songs; {len(todo)} need enrichment.", flush=True)
    print(f"  genius to fetch: {len(g_jobs)} | deezer to fetch: {len(d_jobs)} | cached G={len(gcache)} D={len(dcache)}", flush=True)

    lock = threading.Lock()

    # ---- Phase 1: Deezer (parallel) ----
    def dwork(sid):
        s = songs[sid]["s"]; key = ck(s)
        with lock:
            if key in dcache: return
        res = scrape_deezer(s.get("title", ""), s.get("artist", ""))
        if usable(res):
            with lock: dcache[key] = res
        return res
    if d_jobs and not SKIP_DEEZER:
        print("Phase 1: Deezer...", flush=True)
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(dwork, sid) for sid in d_jobs]
            for i, f in enumerate(as_completed(futs), 1):
                f.result()
                if i % 200 == 0:
                    with lock: json.dump(dcache, open(DCACHE, "w"), ensure_ascii=False)
                    print(f"  deezer {i}/{len(d_jobs)}", flush=True)
        json.dump(dcache, open(DCACHE, "w"), ensure_ascii=False)

    # ---- Phase 2: Genius (SERIAL + backoff) ----
    if g_jobs and not SKIP_GENIUS:
        if not GENIUS_TOKEN:
            print("!! No Genius token (.genius_token / GENIUS_TOKEN). Skipping Genius.", flush=True)
        else:
            print(f"Phase 2: Genius (api.genius.com, token)... {len(g_jobs)} jobs", flush=True)
            # token-authed: parallel is safe; light backoff if it ever 429s
            def gwork(sid):
                s = songs[sid]["s"]; key = ck(s)
                with lock:
                    if key in gcache: return
                for attempt in range(4):
                    res = scrape_genius(s.get("title", ""), s.get("artist", ""))
                    if isinstance(res, dict) and res.get("_error") == "HTTP429":
                        time.sleep(5 * (attempt + 1)); continue
                    with lock:
                        gcache[key] = res
                    return
            with ThreadPoolExecutor(max_workers=5) as ex:
                futs = [ex.submit(gwork, sid) for sid in g_jobs]
                for i, f in enumerate(as_completed(futs), 1):
                    f.result()
                    if i % 100 == 0:
                        with lock: json.dump(gcache, open(GCACHE, "w"), ensure_ascii=False)
                        print(f"  genius {i}/{len(g_jobs)} (cache={len(gcache)})", flush=True)
            json.dump(gcache, open(GCACHE, "w"), ensure_ascii=False)

    # ---- Phase 3: apply ----
    print("Phase 3: applying fills...", flush=True)
    changes = []
    for sid, miss in todo:
        s = songs[sid]["s"]; key = ck(s)
        g = gcache.get(key) or {}
        d = dcache.get(key) or {}
        if not usable(g): g = {}
        if not usable(d): d = {}
        ch = {"id": sid, "title": s.get("title"), "set": {}}

        def fill(field, val):
            if val and (s.get(field) is None or (isinstance(s.get(field), str) and not s.get(field).strip())):
                s[field] = val; ch["set"][field] = val

        fill("producers", g.get("producers"))
        fill("writers", g.get("writers"))
        fill("recordLabel", d.get("label"))
        rd = g.get("release_date") or d.get("release_date")
        if rd and (not s.get("year") or str(s.get("year")).lower() in {"unknown", "0", ""}):
            m = re.match(r'^(\d{4})', str(rd))
            if m: s["year"] = m.group(1); ch["set"]["year"] = m.group(1)
        fill("coverArt", d.get("cover"))

        rtype = (d.get("release_type") or "").lower()
        album = d.get("album") or g.get("album")
        cur = release_formats.get(sid)
        new_fmt = None
        if rtype == "single":
            new_fmt = "Single"
        elif rtype == "ep" and album:
            new_fmt = f"EP: {album}"
        elif album:
            new_fmt = f"Album: {album}"
        if new_fmt:
            if cur is None:
                release_formats[sid] = new_fmt; ch["set"]["format"] = new_fmt
            elif cur == "Single" and new_fmt.startswith(("Album", "EP")) and not ONLY_MISSING_FMT:
                release_formats[sid] = new_fmt; ch["set"]["format"] = f"(was Single) {new_fmt}"

        if ch["set"]:
            changes.append(ch)
            ie = idx_by_id.get(sid)
            if ie is not None:
                if "producers" in ch["set"]: ie["producers"] = s["producers"]
                if "writers" in ch["set"]: ie["writers"] = s["writers"]
                if "coverArt" in ch["set"]: ie["coverArt"] = s["coverArt"]
            with open(songs[sid]["fp"], "w", encoding="utf-8") as f:
                if songs[sid]["pretty"]:
                    json.dump(s, f, indent=2, ensure_ascii=False)
                else:
                    json.dump(s, f, separators=(',', ':'), ensure_ascii=False)

    json.dump(index, open(os.path.join(APP, "music-index.json"), "w", encoding="utf-8"), separators=(',', ':'), ensure_ascii=False)
    json.dump(release_formats, open(os.path.join(APP, "release-formats.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    json.dump(changes, open("enrich_changes.json", "w"), indent=2, ensure_ascii=False)

    # ---- DB sync SQL (COALESCE = fill-only-missing) ----
    def esc(v): return "'" + str(v).replace("'", "''") + "'"
    sql = []
    for ch in changes:
        uuid = num_to_uuid.get(ch["id"])
        if not uuid: continue
        s = songs[ch["id"]]["s"]; sets = []
        for col, field in [("producers", "producers"), ("writers", "writers"), ("record_label", "recordLabel")]:
            if field in ch["set"] and s.get(field):
                sets.append(f"{col} = COALESCE(NULLIF({col},''), {esc(s[field])})")
        if "year" in ch["set"] and s.get("year"):
            sets.append(f"year = COALESCE(year, {int(s['year'])})")
        fmt = release_formats.get(ch["id"], "")
        if fmt.startswith("Album: "):
            sets.append(f"album = COALESCE(NULLIF(album,''), {esc(fmt[7:])})"); sets.append("release_type = COALESCE(NULLIF(release_type,''), 'album')")
        elif fmt.startswith("EP: "):
            sets.append(f"album = COALESCE(NULLIF(album,''), {esc(fmt[4:])})"); sets.append("release_type = COALESCE(NULLIF(release_type,''), 'ep')")
        elif fmt == "Single":
            sets.append("release_type = COALESCE(NULLIF(release_type,''), 'single')")
        if sets:
            sql.append(f"UPDATE tracks SET {', '.join(sets)} WHERE id = '{uuid}';")
    open("db_sync_updates.sql", "w").write("\n".join(sql))

    remaining_g = sum(1 for sid, miss in todo if (miss & {"producers", "writers"}) and not usable(gcache.get(ck(songs[sid]["s"]), {})))
    print(f"\nDONE. changed={len(changes)} songs | db_sync rows={len(sql)} | genius still unresolved={remaining_g}")

if __name__ == "__main__":
    main()
