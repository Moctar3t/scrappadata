#!/usr/bin/env python3
"""DRY RUN catalogue audit — reports what is wrong, changes NOTHING.

Opens no file for writing except its own report. Never touches songs/*.json,
music-index.json or release-formats.json.

Three layers, cheapest first:

  1. STRUCTURAL (offline, 100% of the catalogue, exact)
     Blanks, junk placeholders, impossible values, id consistency across the
     three files, album edition-variant collisions, cover sharing across
     different albums, BPM grid-quantisation and half/double-time suspects.

  2. CONSENSUS (cache-first, network only if asked)
     Album / year / label checked against Deezer + iTunes + MusicBrainz. A field
     is only called wrong when >=2 INDEPENDENT sources agree against the app —
     one source disagreeing is an opinion, not an error.

  3. REACHABILITY (network, sampled)
     Cover-art URLs actually resolve. Sampled, with a binomial confidence
     interval, so a 3924-song verdict costs a few hundred requests.

Usage:
    python3 audit_enrichment.py                      # structural + cached consensus
    python3 audit_enrichment.py --covers 400         # + sample cover reachability
    python3 audit_enrichment.py --net 300            # + fresh lookups for 300 songs
"""
from __future__ import annotations
import argparse, collections, concurrent.futures as cf, glob, json, math, os, random, re
import sys, time, unicodedata, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
SONGS = os.path.join(APP, "songs")
INDEX = os.path.join(APP, "music-index.json")
FORMATS = os.path.join(APP, "release-formats.json")
OUT = os.path.join(HERE, "enrichment_audit.json")

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

# Values that are present in the JSON but carry no information. "None" is the
# giveaway of a Python str(None) leaking into the data.
JUNK = {"none", "null", "nan", "n/a", "na", "-", "--", "?", "unknown", "unknown artist",
        "tbd", "todo", "various", "various artists", "0", "undefined"}
# "ok" was here and looked like an obvious placeholder — it isn't. Verified
# against the raw Genius API: id 260/444/... genuinely credit a producer whose
# handle is "ok" (api.genius.com/songs/9145221 -> producer_artists: ["ok"]).
# Flagging it kept "fixing" real data back into blank. Left out on purpose.


def load(p, d=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return d


def blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def junk(v) -> bool:
    return isinstance(v, str) and v.strip().lower() in JUNK


def norm_album(x: str) -> str:
    """Strip edition noise so 'A Great Chaos' and 'A Great Chaos (Deluxe)' collide."""
    def _clean(s):
        s = unicodedata.normalize("NFKD", str(s or "").lower())
        s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
        s = re.sub(r"\b(deluxe|remaster(ed)?|edition|version|extended|expanded|anniversary"
                   r"|reissue|explicit|clean|bonus|tracks?|directors? cut|hosted by|mixtape)\b",
                   " ", s)
        return s
    stripped = re.sub(r"\b(19|20)\d{2}\b", " ", _clean(x))
    out = re.sub(r"[^a-z0-9]+", "", stripped)
    if out:
        return out
    # A title that IS a bare year ("2024", "1942") strips to empty here, which
    # would make it look like a substring of every other album — fall back to the
    # unstripped normalisation so it only matches itself.
    return re.sub(r"[^a-z0-9]+", "", _clean(x))


def album_of(fmt_value):
    """release-formats stores 'Single' | 'Ep: X' | 'Album: X'."""
    v = str(fmt_value or "")
    for p in ("Album: ", "Ep: ", "EP: "):
        if v.startswith(p):
            return v[len(p):].strip()
    return None


def year_of(v):
    m = re.search(r"(19|20)\d{2}", str(v or ""))
    return int(m.group(0)) if m else None


def wilson(k: int, n: int) -> tuple[float, float]:
    """95% CI for a proportion — so a sampled % is reported honestly."""
    if n == 0:
        return (0.0, 0.0)
    p, z = k / n, 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, (c - h) * 100), min(100.0, (c + h) * 100))


# ════════════════════════════════════════════════════════════════════════════
# 1. STRUCTURAL
# ════════════════════════════════════════════════════════════════════════════
def structural(songs, index, formats):
    n = len(songs)
    R: dict = {"total_songs": n}
    problems = collections.defaultdict(list)      # code -> [song ids]

    def flag(code, sid):
        problems[code].append(sid)

    # ── per-song field quality ──────────────────────────────────────────────
    for s in songs:
        sid = str(s.get("id", "?"))

        for field in ("producers", "writers", "recordLabel"):
            v = s.get(field)
            if blank(v):
                flag(f"{field}.blank", sid)
            elif junk(v):
                flag(f"{field}.junk", sid)

        # "writers == artist" alone is NOT an error — most rap is self-written and
        # Genius legitimately lists only the performer. It only means the row was
        # never enriched when the producer credit is ALSO absent.
        art_whole = str(s.get("artist", "")).strip().lower()
        prod_missing = blank(s.get("producers")) or junk(s.get("producers"))
        wr = str(s.get("writers") or "").strip().lower()
        wr_thin = blank(wr) or junk(wr) or wr == art_whole
        if prod_missing and wr_thin:
            flag("credits.unenriched", sid)          # no producer AND no real writer
        elif prod_missing:
            flag("credits.no_producer_only", sid)

        y = year_of(s.get("year"))
        if blank(s.get("year")):
            flag("year.blank", sid)
        elif y is None:
            flag("year.unparseable", sid)
        elif y < 1900 or y > 2027:
            flag("year.impossible", sid)

        b = s.get("bpm")
        if not b:
            flag("bpm.blank", sid)
        elif not isinstance(b, (int, float)):
            flag("bpm.not_numeric", sid)
        elif b < 55 or b > 210:
            flag("bpm.out_of_range", sid)
        elif b < 78:
            flag("bpm.halftime_suspect", sid)       # 70/72/74 = 140/144/148 halved
        elif b > 185:
            flag("bpm.doubletime_suspect", sid)

        k = str(s.get("key", "")).strip()
        if not k:
            flag("key.blank", sid)
        elif not re.fullmatch(r"[A-G][#b]? ?(Major|Minor)", k):
            flag("key.malformed", sid)

        ca = s.get("coverArt")
        if blank(ca):
            flag("coverArt.blank", sid)
        elif not str(ca).startswith("https://"):
            flag("coverArt.not_https", sid)

        st = s.get("stems") or []
        if len(st) != 4:
            flag("stems.not_four", sid)
        if any(blank(x.get("url")) for x in st):
            flag("stems.missing_url", sid)

        d = s.get("duration")
        if not d or not isinstance(d, (int, float)) or d < 30 or d > 1200:
            flag("duration.implausible", sid)

    # ── cross-file id consistency ───────────────────────────────────────────
    sids = {str(s["id"]) for s in songs}
    iids = {str(s["id"]) for s in index}
    fids = {str(k) for k in formats}
    R["id_consistency"] = {
        "song_files": len(sids),
        "index_entries": len(iids),
        "format_entries": len(fids),
        "in_index_but_no_song_file": sorted(iids - sids),
        "in_song_files_but_not_index": sorted(sids - iids),
        "format_for_nonexistent_song": len(fids - sids),
        "song_with_no_format": sorted(sids - fids),
    }

    # ── song file vs index drift ────────────────────────────────────────────
    by_i = {str(s["id"]): s for s in index}
    drift = collections.Counter()
    drift_ids = collections.defaultdict(list)
    shared = sorted(sids & iids, key=lambda x: int(x) if x.isdigit() else 0)
    for sid in shared:
        a = next(x for x in songs if str(x["id"]) == sid)
        b = by_i[sid]
        for f_a, f_b, name in (("bpm", "bpm", "bpm"), ("key", "key", "key"),
                               ("producers", "producers", "producers"),
                               ("writers", "writers", "writers"),
                               ("coverArt", "coverArt", "coverArt"),
                               ("displayDuration", "duration", "duration")):
            va, vb = a.get(f_a), b.get(f_b)
            if isinstance(va, str) or isinstance(vb, str):
                va, vb = str(va or "").strip(), str(vb or "").strip()
            if va != vb:
                drift[name] += 1
                if len(drift_ids[name]) < 40:
                    drift_ids[name].append(sid)
    R["index_drift"] = {"compared": len(shared), "counts": dict(drift),
                        "examples": dict(drift_ids)}

    # ── index-only enrichment coverage ──────────────────────────────────────
    cov = collections.Counter()
    for b in index:
        for f in ("genres", "valence", "danceability", "energy",
                  "acousticness", "speechiness", "instrumentalness"):
            v = b.get(f)
            if v is None or v == "" or v == []:
                cov[f] += 1
    R["index_missing_fields"] = {"of": len(index), "missing": dict(cov)}

    # ── album layer ─────────────────────────────────────────────────────────
    kinds = collections.Counter(str(v).split(":")[0].strip() for v in formats.values())
    album_eq_title = []
    for s in songs:
        a = album_of(formats.get(str(s["id"])))
        if a and a.strip().lower() == str(s.get("title", "")).strip().lower():
            album_eq_title.append(str(s["id"]))

    by_artist = collections.defaultdict(set)
    for s in songs:
        a = album_of(formats.get(str(s["id"])))
        if a:
            by_artist[str(s.get("artist", "")).split(";")[0].strip().lower()].add(a)
    collisions = []
    for artist, albums in by_artist.items():
        g = collections.defaultdict(set)
        for a in albums:
            g[norm_album(a)].add(a)
        for _, variants in g.items():
            if len(variants) > 1:
                collisions.append({"artist": artist, "variants": sorted(variants)})
    R["albums"] = {
        "kinds": dict(kinds),
        "album_name_equals_song_title": album_eq_title,
        "edition_variant_collisions": collisions,
        "redundant_album_names": sum(len(c["variants"]) - 1 for c in collisions),
    }

    # ── cover art shared across DIFFERENT albums ────────────────────────────
    # Same cover for one album is correct. Same cover for two albums is not.
    by_cover = collections.defaultdict(list)
    for s in songs:
        c = s.get("coverArt")
        if c:
            by_cover[c].append(s)
    cross, cross_songs = [], 0
    for url, group in by_cover.items():
        if len(group) < 2:
            continue
        albums = {norm_album(album_of(formats.get(str(x["id"]))) or "")
                  for x in group}
        albums.discard("")
        singles = [x for x in group
                   if album_of(formats.get(str(x["id"]))) is None]
        # >1 distinct album behind one image, or a mix of singles from different
        # artists sharing one image
        artists = {str(x.get("artist", "")).split(";")[0].strip().lower() for x in group}
        if len(albums) > 1 or (len(artists) > 1 and len(singles) == len(group)):
            cross.append({"url": url, "songs": len(group),
                          "albums": sorted(albums)[:6], "artists": sorted(artists)[:6]})
            cross_songs += len(group)
    cross.sort(key=lambda c: -c["songs"])
    R["cover_sharing"] = {
        "distinct_urls": len(by_cover),
        "urls_shared_by_multiple_songs": sum(1 for g in by_cover.values() if len(g) > 1),
        "shared_across_different_albums": len(cross),
        "songs_affected": cross_songs,
        "worst": cross[:15],
    }
    hosts = collections.Counter(urllib.parse.urlparse(str(s.get("coverArt") or "")).netloc
                                for s in songs)
    R["cover_hosts"] = dict(hosts.most_common())

    # ── BPM grid quantisation ───────────────────────────────────────────────
    # A detector that reports tempo on a logarithmic (semitone) grid gives you
    # 117 / 123 / 129 / 136 / 144 / 152 — each ~5.9% above the last. Anything on
    # that grid is only accurate to +/-3%.
    grid = {round(144 * 2 ** (k / 12)) for k in range(-24, 25)}
    bpms = [(str(s["id"]), s["bpm"]) for s in songs if isinstance(s.get("bpm"), (int, float)) and s["bpm"]]
    on_grid = [i for i, b in bpms if b in grid]
    # chance baseline: how much of the plausible integer range the grid covers
    span = [b for b in range(55, 211)]
    baseline = len([b for b in span if b in grid]) / len(span) * 100
    counts = collections.Counter(b for _, b in bpms)
    R["bpm"] = {
        "with_value": len(bpms),
        "distinct_values": len(counts),
        "top_values": counts.most_common(12),
        "top4_share_pct": round(sum(v for _, v in counts.most_common(4)) * 100 / max(len(bpms), 1), 1),
        "on_semitone_grid": len(on_grid),
        "on_semitone_grid_pct": round(len(on_grid) * 100 / max(len(bpms), 1), 1),
        "chance_baseline_pct": round(baseline, 1),
    }
    # locate the quantised batch by id range
    buckets = collections.Counter()
    totals = collections.Counter()
    for i, b in bpms:
        k = int(i) // 500 if i.isdigit() else -1
        totals[k] += 1
        if b in grid:
            buckets[k] += 1
    R["bpm"]["by_id_bucket"] = {f"{k*500}-{k*500+499}":
                                {"on_grid": buckets.get(k, 0), "total": totals[k],
                                 "pct": round(buckets.get(k, 0) * 100 / totals[k], 1)}
                                for k in sorted(totals) if k >= 0}

    # ── key naming ──────────────────────────────────────────────────────────
    keys = collections.Counter(str(s.get("key", "")).strip() for s in songs if s.get("key"))
    R["key"] = {
        "distinct": len(keys),
        "top": keys.most_common(6),
        "uses_flats": any("b " in k or k.endswith("b") for k in keys),
        "sharp_only": not any(re.match(r"^[A-G]b", k) for k in keys),
        "top_share_pct": round(keys.most_common(1)[0][1] * 100 / max(sum(keys.values()), 1), 1),
    }

    # ── exact duplicates ────────────────────────────────────────────────────
    dupes = collections.defaultdict(list)
    for s in songs:
        dupes[(str(s.get("title", "")).strip().lower(),
               str(s.get("artist", "")).strip().lower())].append(str(s["id"]))
    R["duplicate_songs"] = [{"title": k[0], "artist": k[1], "ids": v}
                            for k, v in dupes.items() if len(v) > 1]

    R["problems"] = {k: sorted(v, key=lambda x: int(x) if x.isdigit() else 0)
                     for k, v in sorted(problems.items())}
    R["problem_counts"] = {k: len(v) for k, v in sorted(problems.items(), key=lambda x: -len(x[1]))}
    return R


# ════════════════════════════════════════════════════════════════════════════
# 2. CONSENSUS (cache-first)
# ════════════════════════════════════════════════════════════════════════════
def consensus_layer(songs, formats, net_budget: int):
    import meta_sources as ms
    import spotify_source as sp

    cache = ms._load_cache()

    def cached_only(fn_name, artist, title):
        key = f"{fn_name}|{ms.norm(artist)}|{ms.norm(title)}"
        return cache.get(key)

    def gather(s, allow_net: bool):
        artist, title = s.get("artist", ""), s.get("title", "")
        out = {}
        for name, fn in (("deezer", ms.deezer_info), ("itunes", ms.itunes_info),
                         ("musicbrainz", ms.musicbrainz_info)):
            ck = {"deezer": "deezer", "itunes": "itunes", "musicbrainz": "mb"}[name]
            hit = cached_only(ck, artist, title)
            if hit:
                out[name] = hit
            elif allow_net:
                try:
                    r = fn(artist, title)
                except Exception:
                    r = None
                if r:
                    out[name] = r
        return out

    def agree_group(pairs):
        groups = []
        for src, val in pairs:
            if not val:
                continue
            for g in groups:
                if sp.album_same(str(g["value"]), str(val)):
                    g["sources"].append(src)
                    break
            else:
                groups.append({"value": val, "sources": [src]})
        if not groups:
            return None, [], 0
        groups.sort(key=lambda g: -len(g["sources"]))
        return groups[0]["value"], groups[0]["sources"], len(groups[0]["sources"])

    stats = collections.Counter()
    findings = []
    net_used = 0
    order = list(songs)
    random.Random(7).shuffle(order)

    for s in order:
        allow = net_used < net_budget
        srcs = gather(s, allow)
        if allow and srcs:
            net_used += 1
        if not srcs:
            stats["no_source"] += 1
            continue
        stats["checked"] += 1
        sid = str(s["id"])
        issues = []

        app_album = album_of(formats.get(sid))
        cand = [(n, r.get("album")) for n, r in srcs.items() if r.get("album")]
        if cand:
            stats["album_checkable"] += 1
            if app_album:
                if not any(sp.album_same(app_album, v) for _, v in cand):
                    best, who, agree = agree_group(cand)
                    if agree >= 2:
                        stats["album_wrong"] += 1
                        issues.append({"field": "album", "app": app_album,
                                       "suggested": best, "sources": who})
                    else:
                        stats["album_unconfirmed"] += 1
            else:
                # App says Single but sources name an album. Deezer/iTunes model a
                # single as a one-track album NAMED AFTER THE TRACK, so that case is
                # agreement, not a conflict. Only a differently-named parent album
                # means the song is genuinely misfiled.
                best, who, agree = agree_group(cand)
                if agree >= 2 and str(formats.get(sid, "")).strip().lower() == "single":
                    t, a = norm_album(s.get("title", "")), norm_album(best)
                    if a and t and (a == t or t in a or a in t):
                        stats["single_release_confirmed"] += 1
                    else:
                        stats["misfiled_single"] += 1
                        issues.append({"field": "album", "app": "Single",
                                       "suggested": best, "sources": who})

        app_year = year_of(s.get("year"))
        years = [(n, year_of(r.get("year") or r.get("release_date"))) for n, r in srcs.items()]
        years = [(n, y) for n, y in years if y]
        if app_year and years:
            stats["year_checkable"] += 1
            backers = [n for n, y in years if app_year - y > 1]
            if len(backers) >= 2:
                stats["year_wrong"] += 1
                issues.append({"field": "year", "app": app_year,
                               "suggested": min(y for _, y in years), "sources": backers})

        app_label = str(s.get("recordLabel") or "").strip()
        cand = [(n, str(r.get("label") or "").strip()) for n, r in srcs.items()
                if str(r.get("label") or "").strip()]
        if app_label and cand:
            stats["label_checkable"] += 1
            if not any(sp.album_same(app_label, v) for _, v in cand):
                best, who, agree = agree_group(cand)
                if agree >= 2:
                    stats["label_wrong"] += 1
                    issues.append({"field": "recordLabel", "app": app_label,
                                   "suggested": best, "sources": who})

        # credits: sources rarely carry producers, so only report presence
        if issues:
            findings.append({"id": sid, "title": s.get("title"), "artist": s.get("artist"),
                             "issues": issues})

    if net_used:
        ms.save_cache()
    return {"stats": dict(stats), "net_lookups": net_used,
            "findings": findings[:400], "findings_total": len(findings)}


# ════════════════════════════════════════════════════════════════════════════
# 3. COVER REACHABILITY (sampled)
# ════════════════════════════════════════════════════════════════════════════
def check_url(url: str) -> int:
    req = urllib.request.Request(url, headers=UA, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            r.read(1)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def covers_layer(songs, sample: int):
    pool = [(str(s["id"]), s.get("coverArt"), s.get("coverArtFallback")) for s in songs
            if s.get("coverArt")]
    random.Random(11).shuffle(pool)
    pool = pool[:sample]
    dead, dead_fb, results = [], [], {}
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(check_url, c): (i, c, f) for i, c, f in pool}
        for fut in cf.as_completed(futs):
            i, c, f = futs[fut]
            code = fut.result()
            results[i] = code
            if code != 200:
                dead.append({"id": i, "code": code, "url": c, "has_fallback": bool(f)})
    # for dead primaries, is the fallback alive?
    if dead:
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            fbs = {str(s["id"]): s.get("coverArtFallback") for s in songs}
            futs = {ex.submit(check_url, fbs[d["id"]]): d for d in dead if fbs.get(d["id"])}
            for fut in cf.as_completed(futs):
                d = futs[fut]
                if fut.result() != 200:
                    dead_fb.append(d["id"])
    lo, hi = wilson(len(dead), len(pool))
    return {"sampled": len(pool), "dead": len(dead),
            "dead_pct": round(len(dead) * 100 / max(len(pool), 1), 2),
            "ci95_pct": [round(lo, 2), round(hi, 2)],
            "dead_with_dead_fallback": len(dead_fb),
            "codes": dict(collections.Counter(d["code"] for d in dead)),
            "examples": dead[:20]}


# ════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--covers", type=int, default=0, help="sample size for cover reachability")
    ap.add_argument("--net", type=int, default=0, help="max fresh network lookups for consensus")
    ap.add_argument("--skip-consensus", action="store_true")
    args = ap.parse_args()

    songs = [load(f) for f in sorted(glob.glob(os.path.join(SONGS, "*.json")))]
    songs = [s for s in songs if s]
    index = (load(INDEX, {}) or {}).get("songsList", [])
    formats = load(FORMATS, {}) or {}

    print(f"catalogue: {len(songs)} song files, {len(index)} index entries, "
          f"{len(formats)} release formats\n")

    report = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
              "mode": "DRY RUN — nothing was modified"}

    t = time.time()
    report["structural"] = structural(songs, index, formats)
    print(f"structural  ... {time.time()-t:.1f}s")

    if not args.skip_consensus:
        t = time.time()
        report["consensus"] = consensus_layer(songs, formats, args.net)
        print(f"consensus   ... {time.time()-t:.1f}s  "
              f"({report['consensus']['stats'].get('checked',0)} songs had sources)")

    if args.covers:
        t = time.time()
        report["covers"] = covers_layer(songs, args.covers)
        print(f"covers      ... {time.time()-t:.1f}s")

    json.dump(report, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nwritten -> {OUT}   (NOTHING was corrected)")

    # ── console summary ─────────────────────────────────────────────────────
    S, n = report["structural"], len(songs)
    print("\n" + "=" * 66)
    print(f"{'FIELD PROBLEM':<34}{'COUNT':>8}{'% CATALOGUE':>14}")
    print("-" * 66)
    for k, v in report["structural"]["problem_counts"].items():
        print(f"{k:<34}{v:>8}{v*100/n:>13.1f}%")
    print("-" * 66)
    ic = S["id_consistency"]
    print(f"{'index entries with no song file':<34}{len(ic['in_index_but_no_song_file']):>8}")
    print(f"{'song files missing from index':<34}{len(ic['in_song_files_but_not_index']):>8}")
    print(f"{'formats for nonexistent songs':<34}{ic['format_for_nonexistent_song']:>8}")
    print(f"{'songs with no release format':<34}{len(ic['song_with_no_format']):>8}"
          f"{len(ic['song_with_no_format'])*100/n:>13.1f}%")
    print(f"{'album name == song title':<34}{len(S['albums']['album_name_equals_song_title']):>8}")
    print(f"{'album edition duplicates':<34}{S['albums']['redundant_album_names']:>8}")
    print(f"{'covers shared across albums':<34}{S['cover_sharing']['shared_across_different_albums']:>8}"
          f"  ({S['cover_sharing']['songs_affected']} songs)")
    print(f"{'BPM on semitone grid':<34}{S['bpm']['on_semitone_grid']:>8}"
          f"{S['bpm']['on_semitone_grid_pct']:>13.1f}%  (chance {S['bpm']['chance_baseline_pct']}%)")
    if "consensus" in report:
        c = report["consensus"]["stats"]
        print("-" * 66)
        for field in ("album", "year", "label"):
            ch, wr = c.get(f"{field}_checkable", 0), c.get(f"{field}_wrong", 0)
            if ch:
                print(f"{field+' wrong (>=2 sources)':<34}{wr:>8}{wr*100/ch:>13.1f}%  of {ch} checkable")
        ch = c.get("album_checkable", 0)
        for k, lbl in (("misfiled_single", "single that belongs to album"),
                       ("album_unconfirmed", "album differs, 1 source only"),
                       ("no_source", "no external source at all")):
            v = c.get(k, 0)
            base = n if k == "no_source" else ch
            print(f"{lbl:<34}{v:>8}{v*100/max(base,1):>13.1f}%")
    if "covers" in report:
        cv = report["covers"]
        print("-" * 66)
        print(f"{'dead cover URLs (sampled)':<34}{cv['dead']:>8}{cv['dead_pct']:>13.2f}%"
              f"  CI {cv['ci95_pct'][0]}–{cv['ci95_pct'][1]}%")
    print("=" * 66)


if __name__ == "__main__":
    main()
