#!/usr/bin/env python3
"""
enrich_producers.py — fix the producer data. The old scrape wrote "Self-Produced"
whenever Genius's structured `producer_artists` was empty, which is mostly FALSE
(Genius often keeps producers under `custom_performances` instead). This:

  1. Targets only songs whose current producer is junk ("Self-Produced", "N/A",
     "Search Pending", empty) — keeps the already-good data.
  2. Re-queries Genius, extracting from `producer_artists` AND production-role
     `custom_performances` (Producer / Co-Producer / Additional Production).
  3. Writes the REAL producer, or "" (empty) when genuinely none — never the
     misleading "Self-Produced".

Updates the app's per-song JSON in place (songs/NNNN.json). Resumable cache.
Re-upload with scripts/upload-catalog.sh afterwards for the CDN copies.
"""
import json, os, re, sys, time, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
INDEX = os.path.join(APP, "music-index.json")
SONGS = os.path.join(APP, "songs")
CACHE = os.path.join(ROOT, "producer_cache_v2.json")

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}

JUNK = {"self-produced", "self produced", "selfproduced", "n/a", "na", "none",
        "null", "search pending", "unknown", "tba", "", "?"}
# custom_performances labels that genuinely mean "made the beat".
PROD_ROLES = {"producer", "co-producer", "additional production", "additional producer"}

def is_junk(p):
    return (p or "").strip().lower() in JUNK

def _get(u):
    return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=8).read().decode())

def genius_producers(artist, title):
    q = urllib.parse.quote(f"{artist} {title}")
    d = _get(f"https://genius.com/api/search/multi?q={q}")
    sid = None
    for s in d.get('response', {}).get('sections', []):
        if s.get('type') == 'song' and s.get('hits'):
            sid = s['hits'][0]['result']['id']; break
    if not sid:
        return ""
    time.sleep(0.1)
    d = _get(f"https://genius.com/api/songs/{sid}")
    song = d['response']['song']
    names = [p['name'] for p in song.get('producer_artists', [])]
    if not names:  # supplement from production-role performances only
        for perf in song.get('custom_performances', []):
            if perf.get('label', '').strip().lower() in PROD_ROLES:
                names += [a['name'] for a in perf.get('artists', [])]
    seen, out = set(), []
    for n in names:
        k = n.strip().lower()
        if k and k not in seen and k not in JUNK:
            seen.add(k); out.append(n.strip())
    return ", ".join(out)

def main():
    cache = {}
    if os.path.exists(CACHE):
        try: cache = json.load(open(CACHE))
        except Exception: cache = {}

    songs = json.load(open(INDEX))["songsList"]
    targets = []  # (filepath, key, artist, title)
    for s in songs:
        sid = s.get("id");  art = s.get("artist", "");  title = s.get("title", "")
        if not sid: continue
        fp = os.path.join(SONGS, f"{int(sid):04d}.json")
        if not os.path.exists(fp): continue
        try: d = json.load(open(fp))
        except Exception: continue
        if is_junk(d.get("producers")):
            targets.append((fp, f"{art} - {title}", art, title))
    print(f"{len(targets)} songs with junk producers to re-scrape.")

    def work(t):
        fp, key, art, title = t
        if key in cache:
            return fp, cache[key]
        try:
            res = genius_producers(art, title)
        except Exception:
            return fp, None  # leave as-is, retry next run
        cache[key] = res
        time.sleep(0.15)
        return fp, res

    fixed = emptied = 0; done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(work, t): t for t in targets}
        for fut in as_completed(futs):
            fp, res = fut.result()
            done += 1
            if res is None:
                pass
            else:
                try:
                    d = json.load(open(fp))
                    d["producers"] = res                 # real producer, or "" (not Self-Produced)
                    json.dump(d, open(fp, "w"), ensure_ascii=False, indent=2)
                    if res: fixed += 1
                    else:   emptied += 1
                except Exception:
                    pass
            if done % 25 == 0:
                json.dump(cache, open(CACHE, "w"), ensure_ascii=False, indent=1)
                print(f"  {done}/{len(targets)}  (fixed {fixed}, emptied {emptied})")

    json.dump(cache, open(CACHE, "w"), ensure_ascii=False, indent=1)
    print(f"Done. Real producers recovered: {fixed}. Set to empty (no data): {emptied}.")

if __name__ == "__main__":
    main()
