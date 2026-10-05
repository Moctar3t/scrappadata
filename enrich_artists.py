#!/usr/bin/env python3
"""
enrich_artists.py — scrape REAL artist profile pictures (not album covers) for
every artist in the iOS catalogue, into a single `artists.json` the app reads
dynamically (cache-first + CDN refresh, same as the catalogue).

Sources, in priority order:
  1. Deezer  (api.deezer.com, no auth) — clean 1000x1000 artist photos + nb_fan
     for disambiguation. Primary.
  2. Genius  (genius.com/api) — primary_artist.image_url as fallback.

Output: artists.json = { normalizedKey: {"name": display, "image": url,
                                          "source": "deezer"|"genius", "fans": int} }
normalizedKey matches the app's ArtistGrouping.normalizeKey (fold + lower +
collapse whitespace), and artist splitting matches ArtistGrouping.artistNames
(split ";" + featured), so every individual artist gets a picture.

Resumable: caches results in artist_image_cache.json; re-run to fill gaps.
"""
import json, os, re, sys, time, unicodedata, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.environ.get("MUSIC_INDEX",
    "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata/music-index.json")
OUT_APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata/artists.json"
CACHE = os.path.join(ROOT, "artist_image_cache.json")

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}

# ---- normalization / splitting (must mirror ArtistGrouping in the app) -------
def fold(s):
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return ' '.join(s.lower().split())

MARKERS = [" (feat", " (ft", " feat.", " feat ", " ft. ", " featuring", " (with "]
def split_feature(seg):
    low = seg.lower(); cut = len(seg)
    for m in MARKERS:
        i = low.find(m)
        if i != -1 and i < cut: cut = i
    primary = seg[:cut].strip()
    if cut >= len(seg): return primary, []
    clause = seg[cut:].replace("(", " ").replace(")", " ")
    feats = []
    for part in re.split(r"[,&]", clause):
        p = part.strip()
        for w in ["featuring ", "feat. ", "feat ", "ft. ", "ft ", "with "]:
            if p.lower().startswith(w): p = p[len(w):]; break
        p = p.strip()
        if p: feats.append(p)
    return primary, feats

def artist_names(raw):
    names = []
    for seg in raw.split(";"):
        seg = seg.strip()
        if not seg: continue
        pri, feats = split_feature(seg)
        if pri: names.append(pri)
        names += feats
    seen, out = set(), []
    for n in names:
        k = fold(n)
        if k and k not in seen: seen.add(k); out.append(n)
    return out or ([raw.strip()] if raw.strip() else [])

# ---- sources ----------------------------------------------------------------
def _get(url, timeout=8):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read().decode())

def deezer_image(name):
    try:
        d = _get(f"https://api.deezer.com/search/artist?q={urllib.parse.quote(name)}&limit=5")
    except Exception:
        return None
    want = fold(name)
    best, best_fans = None, -1
    for a in d.get('data', []):
        if fold(a.get('name', '')) == want and a.get('nb_fan', 0) > best_fans:
            best, best_fans = a, a.get('nb_fan', 0)
    if not best and d.get('data'):  # no exact name match → take most-popular hit
        best = max(d['data'], key=lambda x: x.get('nb_fan', 0))
    if best:
        img = best.get('picture_xl') or best.get('picture_big')
        if img: return {"image": img, "source": "deezer", "fans": best.get('nb_fan', 0)}
    return None

def genius_image(name):
    try:
        d = _get(f"https://genius.com/api/search/multi?q={urllib.parse.quote(name)}")
    except Exception:
        return None
    for s in d.get('response', {}).get('sections', []):
        if s.get('type') == 'artist' and s.get('hits'):
            r = s['hits'][0]['result']
            if r.get('image_url'): return {"image": r['image_url'], "source": "genius", "fans": 0}
    # fall back to a song's primary_artist image
    for s in d.get('response', {}).get('sections', []):
        if s.get('type') == 'song' and s.get('hits'):
            pa = s['hits'][0]['result'].get('primary_artist', {})
            if fold(pa.get('name', '')) == fold(name) and pa.get('image_url'):
                return {"image": pa['image_url'], "source": "genius", "fans": 0}
    return None

def lookup(name):
    return deezer_image(name) or genius_image(name)

# ---- main -------------------------------------------------------------------
def main():
    cache = {}
    if os.path.exists(CACHE):
        try: cache = json.load(open(CACHE))
        except Exception: cache = {}

    songs = json.load(open(INDEX))["songsList"]
    artists = {}  # key -> display name
    for s in songs:
        for n in artist_names(s.get("artist", "")):
            artists.setdefault(fold(n), n)
    todo = [(k, n) for k, n in artists.items() if k not in cache]
    print(f"{len(artists)} distinct artists; {len(todo)} to fetch ({len(cache)} cached).")

    def work(item):
        k, n = item
        res = lookup(n)
        time.sleep(0.15)  # be polite to Deezer
        return k, n, res

    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(work, it): it for it in todo}
        for fut in as_completed(futs):
            k, n, res = fut.result()
            if res: cache[k] = {"name": n, **res}
            done += 1
            if done % 25 == 0:
                json.dump(cache, open(CACHE, "w"), ensure_ascii=False, indent=1)
                print(f"  {done}/{len(todo)}  (hit {len(cache)})")

    json.dump(cache, open(CACHE, "w"), ensure_ascii=False, indent=1)
    # App-facing file: drop the fans field, keep name+image+source.
    out = {k: {"name": v["name"], "image": v["image"], "source": v["source"]}
           for k, v in cache.items() if v.get("image")}
    json.dump(out, open(OUT_APP, "w"), ensure_ascii=False, indent=1)
    print(f"Wrote {len(out)} artist images → {OUT_APP}")

if __name__ == "__main__":
    main()
