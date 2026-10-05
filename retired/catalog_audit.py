#!/usr/bin/env python3
"""
catalog_audit.py — find identity defects across the whole catalogue.

Read-only. It reports; `--apply` is handled by the separate fixer so that
finding and changing are never the same step.

WHAT IT LOOKS FOR

covers   Three independent signals, cheapest first:
         · SHARED  — one image used by songs from 2+ different artists. Album art
           is not shared across artists, so this is a compilation cover or a
           generic Genius artist image. (Found via the Junko Ohashi case: id 52
           carried the art of "TOKYO - CITY POP -", a 27-track Various Artists
           compilation, instead of her own 1984 album.)
         · LOWRES  — Genius serves a small artist image (293x293, 640x640) when
           it has no song art; real song art comes back 1000x1000.
         · COMPILATION — for anything flagged above, ask iTunes what release the
           art belongs to and confirm it is a Various-Artists compilation, then
           propose the earliest non-compilation release by the actual artist.

titles   · CJK — any title still in Japanese script, with a romaji proposal.
           Official romanisations (Genius full_title parenthetical, iTunes
           romanised release) are preferred over transliteration, because
           kanji readings are ambiguous — pykakasi renders 君 as "Kun" where
           the song means "Kimi", and turns the loanword ラヴ into "Ravu"
           rather than "Love".
"""
from __future__ import annotations
import json, re, sys, time, unicodedata, urllib.parse, urllib.request
from collections import defaultdict
from pathlib import Path

REPO = Path("/Users/dime/Developer/ios-zingy")
INDEX = REPO / "App/ZingyApp/Resources/musicdata/music-index.json"
HERE = Path(__file__).resolve().parent
CJK = re.compile(r"[぀-ゟ゠-ヿ㐀-鿿豈-﫿ｦ-ﾟ]")

def log(m): print(m, flush=True)

def load(): return json.loads(INDEX.read_text(encoding="utf-8"))["songsList"]

def primary_artist(a: str) -> str:
    """Only the LEAD artist.

    Comparing the full artist set marks a whole album as suspect: Travis Scott's
    UTOPIA tracks share one cover, correctly, but "Travis Scott" and
    "Travis Scott;Teezo Touchdown" are different sets, so every featured track
    looked like a different artist reusing the image. The lead artist is what
    actually decides whether shared art is plausible."""
    a = re.sub(r"\(feat.*?\)|\[feat.*?\]|feat\.?|ft\.?|featuring", "", (a or "").lower())
    first = re.split(r"[;,&]| x | and |/", a)[0]
    return re.sub(r"[^a-z0-9]", "", first)

def itunes(term, entity="song", limit=8):
    u = (f"https://itunes.apple.com/search?term={urllib.parse.quote(term)}"
         f"&entity={entity}&limit={limit}")
    for _ in range(3):
        try:
            return json.loads(urllib.request.urlopen(u, timeout=20).read()
                              .decode("utf-8", "replace")).get("results", [])
        except Exception:
            time.sleep(1.5)
    return []

# ── covers ──────────────────────────────────────────────────────────────────
def audit_covers(songs, deep=0):
    by_cover = defaultdict(list)
    for s in songs:
        if s.get("coverArt"):
            by_cover[s["coverArt"]].append(s)

    shared = []
    for url, group in by_cover.items():
        artists = {primary_artist(g.get("artist", "")) for g in group}
        distinct = len({a for a in artists if a})
        if distinct >= 2:
            shared.append((url, group, distinct))
    shared.sort(key=lambda x: -len(x[1]))

    lowres = [s for s in songs
              if s.get("coverArt") and re.search(r"\.(\d{2,3})x(\d{2,3})x\d", s["coverArt"])
              and int(re.search(r"\.(\d{2,3})x\d{2,3}x\d", s["coverArt"]).group(1)) < 500]

    log("=" * 78)
    log("COVER AUDIT")
    log("=" * 78)
    n_shared_songs = sum(len(g) for _, g, _ in shared)
    log(f"  SHARED across artists : {len(shared)} image(s) covering {n_shared_songs} songs")
    log(f"  LOW-RES Genius art    : {len(lowres)} songs")
    log("")
    log("  worst shared images (image used by songs from N different artists):")
    for url, group, distinct in shared[:15]:
        log(f"    {len(group):>3} songs / {distinct} artists  {url[:74]}")
        for g in group[:3]:
            log(f"         id {g['id']:>5}  {g.get('artist','')[:26]:26s} {g.get('title','')[:30]}")
        if len(group) > 3:
            log(f"         … and {len(group)-3} more")
    if lowres:
        log("")
        log("  low-res (Genius fell back to an artist image):")
        for s in lowres[:12]:
            dim = re.search(r"\.(\d{2,3}x\d{2,3})x\d", s["coverArt"]).group(1)
            log(f"    id {s['id']:>5} [{dim}] {s.get('artist','')[:24]:24s} {s.get('title','')[:30]}")

    flagged = {s["id"] for _, g, _ in shared for s in g} | {s["id"] for s in lowres}
    out = {"shared": [{"url": u, "artists": d, "ids": [g["id"] for g in gr]}
                      for u, gr, d in shared],
           "lowres_ids": [s["id"] for s in lowres],
           "flagged_ids": sorted(flagged, key=lambda x: int(x) if str(x).isdigit() else 0)}

    if deep:
        log("")
        log(f"  confirming the first {deep} flagged songs against iTunes …")
        confirmed = []
        for s in [x for x in songs if x["id"] in flagged][:deep]:
            res = itunes(f"{s.get('artist','')} {s.get('title','')}")
            if not res:
                continue
            comp = [r for r in res if (r.get("artistName") or "").lower() == "various artists"]
            own = [r for r in res if (r.get("artistName") or "").lower() != "various artists"]
            own.sort(key=lambda r: (r.get("releaseDate") or "9999"))
            if own:
                best = own[0]
                confirmed.append({
                    "id": s["id"], "artist": s.get("artist"), "title": s.get("title"),
                    "current_cover": s.get("coverArt"),
                    "proposed_cover": (best.get("artworkUrl100") or "").replace("100x100bb", "1000x1000bb"),
                    "proposed_album": best.get("collectionName"),
                    "proposed_year": (best.get("releaseDate") or "")[:4],
                    "is_compilation_now": bool(comp),
                })
                log(f"    id {s['id']:>5} {s.get('artist','')[:20]:20s} {s.get('title','')[:24]:24s}"
                    f" -> {(best.get('collectionName') or '')[:30]} ({(best.get('releaseDate') or '')[:4]})")
            time.sleep(0.35)
        out["proposals"] = confirmed
    return out

# ── titles ──────────────────────────────────────────────────────────────────
def official_romaji(artist: str, title: str) -> tuple[str | None, str]:
    """Prefer a romanisation a real source publishes over transliterating."""
    tok_path = HERE / ".genius_token"
    if tok_path.exists():
        try:
            tok = tok_path.read_text(encoding="utf-8").strip()
            u = "https://api.genius.com/search?q=" + urllib.parse.quote(f"{artist} {title}")
            r = urllib.request.Request(u, headers={"Authorization": f"Bearer {tok}"})
            hits = json.loads(urllib.request.urlopen(r, timeout=20).read())["response"]["hits"]
            for h in hits[:4]:
                ft = h["result"].get("title") or ""
                # Genius writes  漢字タイトル (Romanised Title)
                m = re.search(r"[（(]\s*([A-Za-z0-9][^()（）]*?)\s*[)）]\s*$", ft)
                if m and title[:4] in ft:
                    return m.group(1).strip(), "genius"
        except Exception:
            pass
    for r in itunes(f"{artist} {title}", limit=5):
        tn = r.get("trackName") or ""
        if tn and not CJK.search(tn):
            return tn, "itunes"
    try:
        import pykakasi
        conv = pykakasi.kakasi().convert(title)
        s = " ".join(x["hepburn"] for x in conv)
        s = re.sub(r"\s*([・、。,.])\s*", " ", s)
        s = re.sub(r"\s+", " ", s).strip().title()
        return (s or None), "pykakasi"
    except Exception:
        return None, "none"

def audit_titles(songs, resolve=0):
    jp = [s for s in songs if CJK.search(s.get("title", ""))]
    log("")
    log("=" * 78)
    log("TITLE AUDIT — Japanese script")
    log("=" * 78)
    log(f"  {len(jp)} song(s) still have Japanese characters in the title")
    props = []
    todo = jp if resolve == 0 else jp[:resolve]
    for s in todo:
        rom, src = official_romaji(s.get("artist", ""), s["title"])
        props.append({"id": s["id"], "artist": s.get("artist"), "old": s["title"],
                      "romaji": rom, "source": src})
        log(f"    id {s['id']:>5} {s.get('artist','')[:20]:20s} {s['title'][:26]:26s} -> "
            f"{(rom or '??')[:34]:34s} [{src}]")
        time.sleep(0.25)
    return {"japanese": props}

def main():
    songs = load()
    log(f"catalogue: {len(songs)} songs\n")
    deep = 0; resolve = 0
    for i, a in enumerate(sys.argv):
        if a == "--deep": deep = int(sys.argv[i+1])
        if a == "--resolve-titles": resolve = int(sys.argv[i+1])
    rep = {}
    rep.update(audit_covers(songs, deep=deep))
    rep.update(audit_titles(songs, resolve=resolve))
    out = HERE / "catalog_audit_report.json"
    out.write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"\nwrote {out.name}")

if __name__ == "__main__":
    main()
