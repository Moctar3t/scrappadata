#!/usr/bin/env python3
"""
enrich_covers.py — fill in / repair cover art. HEAD-checks every song's coverArt
URL and, for any that's missing or doesn't return 200, scrapes a fresh cover
(iTunes 1000x1000 → Deezer album cover) by artist + title. Updates
music-index.json in place. Resumable cache; re-run anytime.
"""
import json, os, re, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata/music-index.json"
CACHE = os.path.join(ROOT, "cover_cache.json")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

def head_ok(url):
    if not url: return False
    try:
        req = urllib.request.Request(url, method="HEAD", headers=UA)
        return urllib.request.urlopen(req, timeout=6).status == 200
    except Exception:
        # Some CDNs reject HEAD — try a tiny ranged GET before giving up.
        try:
            req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-0"})
            return urllib.request.urlopen(req, timeout=6).status in (200, 206)
        except Exception:
            return False

def _get(url):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=8).read().decode())

def itunes_cover(artist, title):
    try:
        d = _get(f"https://itunes.apple.com/search?term={urllib.parse.quote(f'{artist} {title}')}&entity=song&limit=1")
        if d.get("resultCount"):
            art = d["results"][0].get("artworkUrl100", "")
            if art: return art.replace("100x100bb", "1200x1200bb")
    except Exception:
        pass
    return None

def deezer_cover(artist, title):
    try:
        d = _get(f"https://api.deezer.com/search?q={urllib.parse.quote(f'{artist} {title}')}&limit=1")
        if d.get("data"):
            alb = d["data"][0].get("album", {})
            return alb.get("cover_xl") or alb.get("cover_big")
    except Exception:
        pass
    return None

def scrape(artist, title):
    return itunes_cover(artist, title) or deezer_cover(artist, title)

def main():
    cache = {}
    if os.path.exists(CACHE):
        try: cache = json.load(open(CACHE))
        except Exception: cache = {}

    doc = json.load(open(INDEX))
    songs = doc["songsList"]
    # Only songs that are actually shown (hasStems true) and have search terms.
    targets = [s for s in songs if s.get("hasStems") and (s.get("artist") or "").strip() and (s.get("title") or "").strip()]
    print(f"{len(targets)} playable songs; checking covers…")

    def work(s):
        key = f"{s.get('artist')} - {s.get('title')}"
        cur = (s.get("coverArt") or "").strip()
        if cur and head_ok(cur):
            return s["id"], None  # fine
        if key in cache:
            return s["id"], cache[key]
        new = scrape(s.get("artist"), s.get("title"))
        cache[key] = new or ""
        time.sleep(0.1)
        return s["id"], new

    fixed = checked = 0
    results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(work, s): s for s in targets}
        for fut in as_completed(futs):
            sid, new = fut.result()
            checked += 1
            if new: results[sid] = new
            if checked % 100 == 0:
                json.dump(cache, open(CACHE, "w"), ensure_ascii=False, indent=1)
                print(f"  checked {checked}/{len(targets)}  (repaired {len(results)})")

    for s in songs:
        if s["id"] in results:
            s["coverArt"] = results[s["id"]]
            fixed += 1
    if fixed:
        # Preserve the compact single-line format the file ships in.
        json.dump(doc, open(INDEX, "w"), ensure_ascii=False, separators=(",", ":"))
    json.dump(cache, open(CACHE, "w"), ensure_ascii=False, indent=1)
    print(f"Done. Covers repaired: {fixed}.")

if __name__ == "__main__":
    main()
