#!/usr/bin/env python3
"""Genre refinement for the WHOLE library — better groupement in the app.

The app's genre browser buckets songs by their `genres` tags (GenreBuckets in
HomeFeature.swift). Two failure modes this fixes:
  1. ~60% of songs have NO tags → invisible in every genre group.
  2. Coarse/foreign tags ("Rap/Hip Hop", "musique africaine") don't tokenize the
     way the app matches, or misfile songs entirely.

Sources, in trust order (song-level beats artist-level):
  existing curated tags → Deezer ALBUM genres (song-adjacent; mostly already in
  meta_cache from enrichment) → MusicBrainz ARTIST tags (rich community genres,
  alias-aware — but artist-level, so capped and filtered hard).

Every tag goes through NORMALIZE (split slashes, translate Deezer French buckets,
drop junk like "seen live"/"favorites") so the app's word-token matching works.

Writes: music-index.json + per-song JSONs + the DB genres column (kept in sync so
future exports agree). Run export/publish afterwards — or let auto_ingest do it.

Usage:
    python3 refine_genres.py               # refine everything
    python3 refine_genres.py --dry-run     # show planned changes only
    python3 refine_genres.py --only 125    # one app id (debug)
"""
from __future__ import annotations
import argparse, json, re, subprocess, time, urllib.parse
from pathlib import Path

from meta_sources import (_get_json, _load_cache, _cache_put, save_cache,
                          _mb_artist_id, norm, primary_artist, deezer_info, MB_UA)

HERE = Path(__file__).resolve().parent
REPO = Path("/Users/dime/Developer/ios-zingy")
IDX = REPO / "App/ZingyApp/Resources/musicdata/music-index.json"
SONGS = REPO / "App/ZingyApp/Resources/musicdata/songs"
VPS = "204.168.236.140"

# Deezer/French/odd buckets → app-friendly tags (the app token-matches words).
TRANSLATE = {
    "rap/hip hop": ["rap", "hip-hop"], "r&b contemporain": ["r&b"],
    "soul & funk": ["soul", "funk"], "musique africaine": ["afro"],
    "variété française": ["chanson française"], "rock indé": ["indie rock"],
    "pop indé": ["indie pop"], "électro": ["electro"], "electro pop": ["electropop"],
    "films/jeux vidéo": [], "musiques du monde": [], "comédies musicales": [],
    "jazz manouche": ["jazz"], "dance & edm": ["dance", "edm"],
    "r&b japonais": ["r&b japonais", "city pop"], "rap us": ["rap"],
    "hip hop": ["hip-hop"], "hiphop": ["hip-hop"],
}
# MusicBrainz community tags that are NOT genres.
MB_JUNK = re.compile(r"^(seen live|favorites?|owned|fixme|spotify|check ?out|"
                     r"american|british|english|french|japanese|canadian|nigerian|"
                     r"male|female|.*vocalists?|[0-9]{2,4}s?|misc.*|und)$", re.I)
GENREISH = re.compile(r"rap|drill|grime|trap|hip.?hop|boom bap|r&b|rnb|soul|funk|"
                      r"jazz|blues|gospel|pop|rock|punk|metal|indie|grunge|shoegaze|"
                      r"emo|folk|country|house|techno|electro|edm|dance|garage|dnb|"
                      r"drum and bass|dubstep|synth|new wave|disco|reggae|dancehall|"
                      r"afro|amapiano|zouk|coupé|latin|reggaeton|salsa|bachata|bossa|"
                      r"chanson|city pop|j-?pop|k-?pop|kayōkyoku|shibuya|enka|"
                      r"ambient|classical|orchestr|lo-?fi|plugg|jersey|phonk|cloud|"
                      r"alté|urbaine|ivoirien|mélodique", re.I)


def normalize(tag: str) -> list[str]:
    t = re.sub(r"\s+", " ", (tag or "").strip().lower())
    if not t or len(t) > 32:
        return []
    if t in TRANSLATE:
        return TRANSLATE[t]
    if "/" in t:                      # "rap/hip hop"-style compounds
        out = []
        for part in t.split("/"):
            out += normalize(part)
        return out
    if MB_JUNK.match(t) or not GENREISH.search(t):
        return []
    return [t]


def mb_artist_tags(artist: str) -> list[str]:
    """Top MusicBrainz community tags for the artist (alias-aware id), filtered to
    actual genres. Cached per artist — a discography costs one lookup."""
    a = primary_artist(artist)
    key = f"mbtags|{norm(a)}"
    if key in _load_cache():
        return _load_cache()[key].get("t", [])
    mbid = _mb_artist_id(a)
    if not mbid:
        return []
    try:
        d = _get_json(f"https://musicbrainz.org/ws/2/artist/{mbid}?fmt=json&inc=tags", MB_UA)
    except Exception:
        return []
    time.sleep(1.1)
    tags = sorted(d.get("tags", []), key=lambda x: -int(x.get("count", 0)))
    out = []
    for tg in tags:
        if int(tg.get("count", 0)) < 1:
            continue
        out += normalize(tg.get("name", ""))
    out = list(dict.fromkeys(out))[:6]
    _cache_put(key, {"t": out})
    return out


def refine(song: dict) -> list[str]:
    artist, title = song.get("artist", ""), song.get("title", "")
    tags: list[str] = []
    for g in song.get("genres") or []:      # curated tags first — they're song-level
        tags += normalize(g)
    if len(tags) < 2:                        # thin → pull Deezer album genres
        try:
            dz = deezer_info(artist, title)
        except Exception:
            dz = None
        for g in (dz or {}).get("genres") or []:
            tags += normalize(g)
    if len(tags) < 2:                        # still thin → MB artist tags (capped)
        tags += mb_artist_tags(artist)[:4]
    return list(dict.fromkeys(tags))[:5]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only")
    a = ap.parse_args()

    catalog = json.load(open(IDX, encoding="utf-8"))
    songs = catalog["songsList"]
    if a.only:
        songs = [s for s in songs if str(s.get("id")) == str(a.only)]
    id_map = {}
    try:
        id_map = {v: k for k, v in json.load(open(HERE / "catalog_id_map.json",
                                                  encoding="utf-8")).items()}
    except Exception:
        pass

    changed, filled, sql = 0, 0, []
    for i, s in enumerate(songs, 1):
        before = s.get("genres") or []
        after = refine(s)
        if i % 100 == 0:
            print(f"  …{i}/{len(songs)} (changed {changed}, filled {filled})")
            save_cache()
        if after == before:
            continue
        if not before and after:
            filled += 1
        changed += 1
        if a.dry_run:
            print(f"  #{s.get('id')} {s.get('artist','?')[:30]} — {s.get('title','?')[:30]}"
                  f"  {before} → {after}")
            continue
        s["genres"] = after
        # per-song detail file
        sid = str(s.get("id"))
        p = SONGS / f"{int(sid):04d}.json" if sid.isdigit() else None
        if p and p.exists():
            try:
                d = json.load(open(p, encoding="utf-8"))
                d["genres"] = after
                json.dump(d, open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
            except Exception:
                pass
        # keep the DB in sync so exports agree
        uuid = id_map.get(sid)
        if uuid and after:
            g = ",".join(after).replace("'", "''")
            sql.append(f"UPDATE tracks SET genres='{g}' WHERE id='{uuid}';")

    if not a.dry_run:
        json.dump(catalog, open(IDX, "w", encoding="utf-8"),
                  separators=(",", ":"), ensure_ascii=False)
        if sql:
            tmp = "/tmp/genres_sync.sql"
            open(tmp, "w", encoding="utf-8").write("\n".join(sql))
            subprocess.run(f"cat {tmp} | ssh root@{VPS} \"sudo -u postgres psql -d zingy -q\"",
                           shell=True, check=True)
    save_cache()
    print(f"\n[genres] {changed} song(s) changed, {filled} previously-untagged now grouped"
          + (" (dry-run)" if a.dry_run else f", {len(sql)} DB rows synced"))
    print("[genres] next: python3 export_catalog.py && bash "
          f"{REPO}/scripts/upload-catalog.sh   (or just run auto_ingest next time)")


if __name__ == "__main__":
    main()
