#!/usr/bin/env python3
"""Prefer Genius cover art (strict title+artist match only), preserving the
existing cover as `coverArtFallback`. One search request per song (the search
hit already carries song_art_image_url). Resumable via genius_cover_cache.json.
"""
import json, os, re, glob, urllib.request, urllib.parse, urllib.error, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from enrich_app_catalog import clean_query_params, norm

APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
SONGS = os.path.join(APP, "songs")
CCACHE = "/Users/dime/Developer/scrappadata/genius_cover_cache.json"
TOKEN = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".genius_token")).read().strip()
H = {'Authorization': f'Bearer {TOKEN}', 'User-Agent': 'Zingy/1.0'}
BAD = ("default_cover", "default_avatar", "default_album")

# Genius answers `song_art_image_url` even when it has no real artwork, handing back a
# 72x72 avatar-sized image that then shipped as a full album cover ("l'histoire de ta
# vie", id 97, was one). The field was never the problem — the field is correct; the
# VALUE needs checking. And the size cannot be rewritten out of trouble: asking Genius
# for a larger variant of the same hash returns 403, because each size is served as its
# own asset.
MIN_COVER_PX = 300

def cover_px(url: str):
    """Short-edge pixels a cover URL advertises, or None when it doesn't say.

    Every source encodes the size somewhere: Genius in `.WxHxN.ext`, Apple and Deezer
    in the path, Spotify in the image-id prefix. Unknown returns None and is ACCEPTED —
    refusing what we cannot measure would throw away good covers to avoid bad ones."""
    if not url:
        return None
    m = re.search(r"\.(\d+)x(\d+)x\d+\.(?:jpg|jpeg|png|gif)$", url, re.I)
    if m: return int(m.group(1))
    m = re.search(r"/(\d+)x(\d+)(?:bb)?\.jpg", url, re.I)
    if m: return int(m.group(1))
    for pre, size in (("ab67616d0000b273", 640), ("ab67616d00001e02", 300), ("ab67616d00004851", 64)):
        if pre in url: return size
    m = re.search(r"/(\d+)x(\d+)-", url)
    if m: return int(m.group(1))
    return None

def usable_cover(url: str) -> bool:
    """A cover we would be happy to show full-screen."""
    if not url or any(b in url for b in BAD):
        return False
    px = cover_px(url)
    return px is None or px >= MIN_COVER_PX

def search_cover(title, artist):
    """Return (cover_url, strict) — strict True only on a confident title+artist match."""
    ct, ca = clean_query_params(title, artist)
    nt = norm(ct)
    try:
        req = urllib.request.Request('https://api.genius.com/search?q=' + urllib.parse.quote(f"{ca} {ct}".strip()), headers=H)
        with urllib.request.urlopen(req, timeout=15) as r:
            hits = json.loads(r.read().decode()).get('response', {}).get('hits', [])
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP{e.code}"}
    except Exception as e:
        return {"_error": type(e).__name__}
    first_artist_match = None
    for hit in hits:
        res = hit.get('result', {})
        ht = norm(res.get('title', '')); ha = norm(res.get('primary_artist', {}).get('name', ''))
        artist_ok = bool(not ca or norm(ca)[:5] in ha or ha[:5] in norm(ca) or ha in norm(artist or ''))
        strict = bool(nt and ht and (nt in ht or ht in nt) and artist_ok)
        if strict:
            url = res.get('song_art_image_url') or ''
            if usable_cover(url):
                return {"cover": url, "strict": True}
            # A match with unusable art is NOT a cover. Saying so lets the caller fall
            # through to another source instead of banking a thumbnail.
            return {"_nocover": True}
        if artist_ok and not first_artist_match:
            url = res.get('song_art_image_url') or ''
            if usable_cover(url):
                first_artist_match = {"cover": url, "strict": False}
    if first_artist_match:
        return first_artist_match
    return {"_nomatch": True}

def main():
    cache = {}
    if os.path.exists(CCACHE):
        try: cache = json.load(open(CCACHE))
        except Exception: cache = {}
    lock = threading.Lock()

    index = json.load(open(os.path.join(APP, "music-index.json"), encoding="utf-8"))
    idx_by_id = {str(s.get("id")): s for s in index.get("songsList", [])}

    songs = {}
    for fp in sorted(glob.glob(os.path.join(SONGS, "*.json"))):
        raw = open(fp, encoding="utf-8").read()
        s = json.loads(raw)
        if (s.get("title") or "").strip():
            songs[str(s.get("id"))] = {"fp": fp, "s": s, "pretty": "\n  " in raw}

    def ck(s): return f"{norm((s.get('artist') or '').split(';')[0])}|{norm(s.get('title'))}"
    jobs = [sid for sid, d in songs.items() if ck(d["s"]) not in cache]
    print(f"{len(songs)} songs; fetching covers for {len(jobs)} (cached {len(cache)})", flush=True)

    def work(sid):
        s = songs[sid]["s"]; key = ck(s)
        with lock:
            if key in cache: return
        res = search_cover(s.get("title", ""), s.get("artist", ""))
        if "_error" not in res:
            with lock: cache[key] = res
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(work, sid) for sid in jobs]
        for i, f in enumerate(as_completed(futs), 1):
            f.result()
            if i % 200 == 0:
                with lock: json.dump(cache, open(CCACHE, "w"), ensure_ascii=False)
                print(f"  covers {i}/{len(jobs)}", flush=True)
    json.dump(cache, open(CCACHE, "w"), ensure_ascii=False)

    # apply
    replaced = filled = kept = 0
    changed_ids = []
    for sid, d in songs.items():
        s = d["s"]; key = ck(s)
        entry = cache.get(key) or {}
        cover = entry.get("cover")
        if not cover:
            kept += 1; continue
        cur = (s.get("coverArt") or "").strip()
        if cur == cover:
            kept += 1; continue
        if cur:
            if not (s.get("coverArtFallback") or "").strip():
                s["coverArtFallback"] = cur
            replaced += 1
        else:
            filled += 1
        s["coverArt"] = cover
        changed_ids.append(sid)
        ie = idx_by_id.get(sid)
        if ie is not None:
            ie["coverArt"] = cover
        with open(d["fp"], "w", encoding="utf-8") as f:
            if d["pretty"]:
                json.dump(s, f, indent=2, ensure_ascii=False)
            else:
                json.dump(s, f, separators=(',', ':'), ensure_ascii=False)

    json.dump(index, open(os.path.join(APP, "music-index.json"), "w", encoding="utf-8"), separators=(',', ':'), ensure_ascii=False)
    json.dump(changed_ids, open("cover_changes.json", "w"))
    nomatch = sum(1 for v in cache.values() if v.get("_nomatch") or v.get("_nocover"))
    print(f"\nDONE covers: replaced={replaced} filled-missing={filled} kept(no strict match/cover)={kept} | genius no-match/no-cover={nomatch}")

if __name__ == "__main__":
    main()
