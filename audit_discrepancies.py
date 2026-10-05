#!/usr/bin/env python3
"""Record where the app's catalogue disagrees with independent sources.

RECORDS ONLY. Never edits a song file, never touches release-formats.json.
Output is `metadata_discrepancies.json`, a reviewable list you decide on later.

Why this is more trustworthy than the earlier passes:

  * CONSENSUS, not a single oracle. A field is only called wrong when at least
    two INDEPENDENT sources agree against the app. One API disagreeing is an
    opinion — Spotify alone produced things like "A Night At The Opera
    (2011 Remaster)" vs "(Deluxe Remastered)" flagged as an album error.
  * EARLIEST release wins for year. Top search hits skew to remasters and
    compilations whose release_date is the REISSUE, which manufactures errors
    and, if ever applied, would overwrite correct data with a reissue date.
  * Edition-aware album comparison. "Deluxe", "Remaster", "Hosted by", a
    trailing year — all normalised away before two album titles are called
    different.
  * Every finding carries its evidence (which sources said what) and a
    confidence, so nothing has to be taken on faith.
  * Deliberately NOT Spotify. That app is rate-limited into next day; Deezer,
    iTunes and MusicBrainz are independent of it and of each other.

Usage:
    python3 audit_discrepancies.py                # whole catalogue
    python3 audit_discrepancies.py --limit 50     # sample
    python3 audit_discrepancies.py --ids 28,1061  # specific songs
"""
from __future__ import annotations
import argparse, glob, json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meta_sources as ms
import spotify_source as sp          # imported ONLY for its album/text normalisers

APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
SONGS = os.path.join(APP, "songs")
RELEASE_FORMATS = os.path.join(APP, "release-formats.json")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata_discrepancies.json")
PROGRESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_progress.txt")


def load(p, d=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return d


def year_of(v):
    m = re.search(r"(19|20)\d{2}", str(v or ""))
    return int(m.group(0)) if m else None


def album_of_format(fmt):
    """release-formats stores 'Single' | 'Ep: X' | 'Album: X'."""
    if not fmt:
        return None
    for prefix in ("Album: ", "Ep: ", "EP: "):
        if fmt.startswith(prefix):
            return fmt[len(prefix):]
    return None          # 'Single' carries no album name


def gather(artist, title):
    """Independent opinions. A source that errors is simply absent — never
    treated as evidence either way."""
    out = {}
    for name, fn in (("deezer", ms.deezer_info),
                     ("itunes", ms.itunes_info),
                     ("musicbrainz", ms.musicbrainz_info)):
        try:
            r = fn(artist, title)
        except Exception:
            r = None
        if r:
            out[name] = r
    return out


def consensus(values):
    """Group equivalent opinions; return (best_value, [sources], agreement)."""
    groups = []
    for src, val in values:
        if not val:
            continue
        placed = False
        for g in groups:
            if sp.album_same(str(g["value"]), str(val)):
                g["sources"].append(src)
                placed = True
                break
        if not placed:
            groups.append({"value": val, "sources": [src]})
    if not groups:
        return None, [], 0
    groups.sort(key=lambda g: -len(g["sources"]))
    return groups[0]["value"], groups[0]["sources"], len(groups[0]["sources"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", type=str, default="")
    args = ap.parse_args()

    formats = load(RELEASE_FORMATS, {}) or {}
    files = sorted(glob.glob(os.path.join(SONGS, "*.json")))
    if args.ids:
        want = {i.strip() for i in args.ids.split(",")}
        files = [f for f in files
                 if str((load(f) or {}).get("id")) in want]
    elif args.limit:
        files = files[: args.limit]

    findings, stats = [], {
        "checked": 0, "no_sources": 0,
        "album_conflict": 0, "year_conflict": 0, "label_conflict": 0,
        "album_unconfirmed": 0,
    }
    t0 = time.time()

    for i, fp in enumerate(files):
        s = load(fp)
        if not s:
            continue
        stats["checked"] += 1
        sid, title, artist = str(s.get("id", "")), s.get("title", ""), s.get("artist", "")

        srcs = gather(artist, title)
        if not srcs:
            stats["no_sources"] += 1
            continue

        issues = []

        # ── album ──────────────────────────────────────────────────────────
        app_album = album_of_format(formats.get(sid))
        if app_album:
            cand = [(n, r.get("album")) for n, r in srcs.items() if r.get("album")]
            best, who, agree = consensus(cand)
            if best and not any(sp.album_same(app_album, v) for _, v in cand):
                issues.append({
                    "field": "album", "app": app_album, "suggested": best,
                    "agreeing_sources": who, "agreement": agree,
                    "confidence": "high" if agree >= 2 else "low",
                    "evidence": {n: v for n, v in cand},
                })
                stats["album_conflict"] += 1
                if agree < 2:
                    stats["album_unconfirmed"] += 1

        # ── year (earliest known release wins) ──────────────────────────────
        app_year = year_of(s.get("year"))
        years = [(n, year_of(r.get("year") or r.get("release_date")))
                 for n, r in srcs.items()]
        years = [(n, y) for n, y in years if y]
        if app_year and years:
            earliest = min(y for _, y in years)
            # Only a conflict when the app is dated AFTER something demonstrably
            # released earlier, and two sources back that.
            backers = [n for n, y in years if app_year - y > 1]
            if len(backers) >= 2:
                issues.append({
                    "field": "year", "app": app_year, "suggested": earliest,
                    "agreeing_sources": backers, "agreement": len(backers),
                    "confidence": "high",
                    "evidence": {n: y for n, y in years},
                })
                stats["year_conflict"] += 1

        # ── label ──────────────────────────────────────────────────────────
        app_label = (s.get("recordLabel") or "").strip()
        if app_label:
            cand = [(n, (r.get("label") or "").strip())
                    for n, r in srcs.items() if (r.get("label") or "").strip()]
            best, who, agree = consensus(cand)
            if best and agree >= 2 and not any(sp.album_same(app_label, v) for _, v in cand):
                issues.append({
                    "field": "recordLabel", "app": app_label, "suggested": best,
                    "agreeing_sources": who, "agreement": agree,
                    "confidence": "high",
                    "evidence": {n: v for n, v in cand},
                })
                stats["label_conflict"] += 1

        if issues:
            findings.append({
                "id": sid, "title": title, "artist": artist,
                "file": os.path.basename(fp), "issues": issues,
            })

        if i % 20 == 0:
            el = time.time() - t0
            line = (f"[{i+1}/{len(files)}] flagged={len(findings)} "
                    f"albumX={stats['album_conflict']} yearX={stats['year_conflict']} "
                    f"labelX={stats['label_conflict']} "
                    f"eta={((len(files)-i-1)/max((i+1)/max(el,.001),.001))/60:.1f}min")
            print(line, flush=True)
            open(PROGRESS, "w").write(line + "\n")

    ms.save_cache()
    json.dump({"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
               "note": "RECORDED ONLY — nothing was corrected.",
               "stats": stats, "findings": findings},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    hi = sum(1 for f in findings for i in f["issues"] if i["confidence"] == "high")
    print("\n" + "=" * 58)
    print(f"checked            : {stats['checked']}")
    print(f"no source matched  : {stats['no_sources']}")
    print(f"songs flagged      : {len(findings)}")
    print(f"  album conflicts  : {stats['album_conflict']}  ({stats['album_unconfirmed']} single-source only)")
    print(f"  year conflicts   : {stats['year_conflict']}")
    print(f"  label conflicts  : {stats['label_conflict']}")
    print(f"high-confidence    : {hi}  (>=2 independent sources agree)")
    print(f"\nrecorded -> {OUT}   (nothing corrected)")


if __name__ == "__main__":
    main()
