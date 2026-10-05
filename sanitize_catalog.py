#!/usr/bin/env python3
"""Content-mismatch sanitiser. We can't listen to the audio, so we triangulate
whether each song is the REAL recording from cheap signals:

  1. Duration vs the canonical Deezer release for that exact artist+title.
     A cover / remix / live / sped-up / wrong-song version almost always has a
     different length than the official studio release.
  2. Source-video title markers (legacy songs encode the YouTube source folder
     in their stem URL): cover / remix / slowed / nightcore / 8d / instrumental…
  3. Artist mismatch: the catalog artist doesn't appear in the source folder.

Read-only. Writes sanitize_report.json + a ranked summary. Nothing is changed.
"""
import json, os, re, glob, urllib.request, urllib.parse, urllib.error, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, unquote

APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
SONGS = os.path.join(APP, "songs")

STRONG = ["cover", "remix", "sped up", "spedup", "speed up", "slowed", "nightcore",
          "8d audio", "8d ", "karaoke", "instrumental", "mashup", "lofi", "lo-fi",
          "ai cover", "bootleg", "parody", "tribute", "reverb", "flip", "rework"]
WEAK = ["live", "acoustic", "demo", "snippet", "edit", "remaster", "session", "unplugged"]

def norm(t): return re.sub(r'[^a-z0-9]', '', (t or '').lower())
def artist_tokens(a):
    a = (a or '').lower()
    a = re.sub(r'\(feat.*?\)|\[feat.*?\]|feat\.?|ft\.?|featuring', '', a)
    return {norm(p) for p in re.split(r'[;,&]| x | and ', a) if norm(p)}

def source_title(song):
    """Legacy stem URLs embed 'Artist - Title (...)' as the folder/filename."""
    st = song.get("stems") or []
    if not st: return None
    u = st[0].get("url")
    if not isinstance(u, str): return None
    p = urlparse(u).path
    if "/catalogtracks/" in p:  # DB stems are UUID-named — no source title here
        return None
    parts = [unquote(x) for x in p.split('/') if x]
    if len(parts) >= 2:
        # folder name (often "Artist - Title ..."), else filename stem
        return parts[-2] if parts[-2] else parts[-1]
    return None

def dz_search(artist, title):
    ca = (artist or "").split(';')[0].strip()
    ca = re.split(r'\(feat|feat\.|ft\.| & ', ca)[0].strip()
    ct = re.sub(r'\(feat.*?\)|\[feat.*?\]', '', title or '').strip()
    try:
        u = 'https://api.deezer.com/search?q=' + urllib.parse.quote(f'{ca} {ct}') + '&limit=8'
        data = json.loads(urllib.request.urlopen(u, timeout=12).read())
    except Exception as e:
        return {"_error": type(e).__name__}
    nt, ats = norm(ct), artist_tokens(artist)
    best = None
    for t in data.get('data', []):
        dt, da = norm(t.get('title', '')), norm(t.get('artist', {}).get('name', ''))
        title_ok = nt and (nt in dt or dt in nt)
        artist_ok = (not ats) or any(tok in da or da in tok for tok in ats if tok)
        if title_ok and artist_ok:
            best = t; break
    if not best:
        return {"_nomatch": True}
    return {"dz_artist": best['artist']['name'], "dz_title": best['title'], "dz_dur": best.get('duration', 0)}

def main():
    songs = []
    for fp in sorted(glob.glob(os.path.join(SONGS, "*.json"))):
        s = json.load(open(fp, encoding="utf-8"))
        if (s.get("title") or "").strip():
            songs.append(s)

    cache = {}
    cf = "sanitize_dz_cache.json"
    if os.path.exists(cf):
        try: cache = json.load(open(cf))
        except Exception: cache = {}
    lock = threading.Lock()

    def key(s): return f"{norm((s.get('artist') or '').split(';')[0])}|{norm(s.get('title'))}"
    todo = [s for s in songs if key(s) not in cache]
    print(f"{len(songs)} songs; Deezer-checking {len(todo)} (cached {len(cache)})", flush=True)

    def work(s):
        k = key(s)
        with lock:
            if k in cache: return
        r = dz_search(s.get("artist", ""), s.get("title", ""))
        if "_error" not in r:
            with lock: cache[k] = r
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(work, s) for s in todo]
        for i, f in enumerate(as_completed(futs), 1):
            f.result()
            if i % 300 == 0:
                with lock: json.dump(cache, open(cf, "w"), ensure_ascii=False)
                print(f"  deezer {i}/{len(todo)}", flush=True)
    json.dump(cache, open(cf, "w"), ensure_ascii=False)

    def has_marker(text, markers):
        t = (text or "").lower()
        return [m for m in markers if re.search(r'(?<![a-z0-9])' + re.escape(m).replace(r'\ ', r'\s*') + r'(?![a-z0-9])', t)]

    alt_versions = []   # explicitly a remix/cover/sped-up/etc — confidently NOT the original
    dur_suspects = []   # plausibly the wrong recording (duration off, title looks original)
    for s in songs:
        sid = str(s.get("id"))
        cat_dur = s.get("duration") or 0
        title = s.get("title") or ""
        src = source_title(s) or ""
        dz = cache.get(key(s), {})

        # marker scan across BOTH the catalog title and the source-video folder
        strong = sorted(set(has_marker(title, STRONG) + has_marker(src, STRONG)))
        weak_in_title = has_marker(title, WEAK)
        title_is_alt = bool(strong or weak_in_title)

        if strong:
            where = "title" if has_marker(title, STRONG) else "source"
            alt_versions.append({"id": sid, "artist": s.get("artist"), "title": title,
                                 "markers": strong, "where": where, "source": src})
            continue

        # duration anomaly — only meaningful when the title doesn't itself say "alt version"
        if not title_is_alt and dz.get("dz_dur") and cat_dur:
            diff = abs(cat_dur - dz["dz_dur"])
            thr = max(30, 0.20 * dz["dz_dur"])   # generous: avoid radio-edit/remaster noise
            if diff > thr:
                dur_suspects.append({"id": sid, "artist": s.get("artist"), "title": title,
                                     "cat_dur": cat_dur, "canon_dur": dz["dz_dur"], "delta": diff,
                                     "dz": f"{dz.get('dz_artist')} - {dz.get('dz_title')}", "source": src})

    dur_suspects.sort(key=lambda x: -x["delta"])
    report = {"alt_versions": alt_versions, "duration_suspects": dur_suspects}
    json.dump(report, open("sanitize_report.json", "w"), indent=2, ensure_ascii=False)

    print(f"\n===== SANITISE / {len(songs)} songs =====")
    print(f"A) EXPLICIT ALT-VERSIONS (remix/cover/sped-up/etc — not the original): {len(alt_versions)}")
    from collections import Counter
    mc = Counter(m for a in alt_versions for m in a["markers"])
    print("   markers:", dict(mc))
    for a in alt_versions[:30]:
        print(f"   {a['id']:>5} {a['artist'][:22]:22} - {a['title'][:30]:30} [{','.join(a['markers'])} @{a['where']}]")
    print(f"\nB) DURATION ANOMALIES (title looks original but length ≠ canonical — possible wrong audio): {len(dur_suspects)}")
    for d in dur_suspects[:30]:
        print(f"   {d['id']:>5} {d['artist'][:20]:20} - {d['title'][:22]:22} | {d['cat_dur']}s vs {d['canon_dur']}s (Δ{d['delta']}s) | DZ:{d['dz'][:30]}")
    print("\nWrote sanitize_report.json")

if __name__ == "__main__":
    main()
