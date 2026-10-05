#!/usr/bin/env python3
"""Sweep every app song against Spotify: measure the real error rate, then fix.

Two phases, deliberately separate — you look at the report before anything is
written:

    python3 spotify_sweep.py            # report only, touches nothing
    python3 spotify_sweep.py --apply    # write the SAFE fixes back

What counts as an error here (not just "missing"):
  MISSING    field absent and Spotify has it            → fillable
  MISMATCH   field present but disagrees with Spotify   → suspect
  NOMATCH    no validated Spotify hit at all            → can't judge

Only high-confidence fixes are applied: year (when absent or off by >1 with a
confident match), recordLabel (absent), coverArt (absent), and release format
(absent). Titles/artists are NEVER rewritten — those are the user's canonical
naming and a "fix" there would rename the catalogue out from under the app.

Resumable: Spotify responses are cached on disk, so a re-run picks up instantly.
"""
from __future__ import annotations
import argparse, glob, json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spotify_source as sp

APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
SONGS = os.path.join(APP, "songs")
RELEASE_FORMATS = os.path.join(APP, "release-formats.json")
REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spotify_sweep_report.json")
PROGRESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spotify_sweep_progress.txt")


def load(p, d=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return d


def year_of(s):
    m = re.search(r"(19|20)\d{2}", str(s or ""))
    return int(m.group(0)) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write safe fixes back")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(SONGS, "*.json")))
    if args.limit:
        files = files[: args.limit]
    formats = load(RELEASE_FORMATS, {}) or {}

    stats = {
        "total": 0, "matched": 0, "nomatch": 0, "api_errors": 0,
        "year_missing_filled": 0, "year_mismatch": 0, "year_app_older": 0,
        "label_missing_filled": 0, "label_mismatch": 0,
        "cover_missing_filled": 0,
        "format_missing_filled": 0,
        "album_mismatch": 0,
    }
    findings = []
    changed_files = 0
    t0 = time.time()

    for i, fp in enumerate(files):
        s = load(fp)
        if not s:
            continue
        stats["total"] += 1
        sid = str(s.get("id", ""))
        title = s.get("title", "")
        artist = s.get("artist", "")

        try:
            hit = sp.find_track(title, artist)
        except sp.TransientError as e:
            stats["api_errors"] += 1
            findings.append({"id": sid, "title": title, "artist": artist,
                             "issue": "API_ERROR", "detail": str(e)})
            if i % 25 == 0:
                _tick(i, len(files), t0, stats)
            continue
        if not hit:
            stats["nomatch"] += 1
            findings.append({"id": sid, "title": title, "artist": artist, "issue": "NOMATCH"})
            if i % 25 == 0:
                _tick(i, len(files), t0, stats)
            continue
        stats["matched"] += 1

        alb = sp.album_details(hit.get("album_id")) or {}
        issues = []
        patch = {}

        # ── year ───────────────────────────────────────────────────────────
        # Use the EARLIEST validated pressing, not the top hit (which is often a
        # remaster). Only flag when the app year is LATER than Spotify's earliest
        # by >1 — the app claiming a date after the song demonstrably existed is a
        # real error (Bohemian Rhapsody filed under 2018). The reverse (app older
        # than anything on Spotify) is usually Spotify lacking the original press,
        # so it's reported but never auto-applied.
        sy = hit.get("earliest_year") or year_of(hit.get("release_date"))
        my = year_of(s.get("year"))
        if sy:
            if not my:
                issues.append({"field": "year", "kind": "MISSING", "spotify": sy})
                patch["year"] = str(sy)
                stats["year_missing_filled"] += 1
            elif my - sy > 1:
                issues.append({"field": "year", "kind": "MISMATCH", "app": my,
                               "spotify": sy, "confident": True})
                stats["year_mismatch"] += 1
            elif sy - my > 1:
                issues.append({"field": "year", "kind": "SUSPECT_APP_OLDER",
                               "app": my, "spotify": sy})
                stats["year_app_older"] += 1

        # ── record label ───────────────────────────────────────────────────
        slabel = (alb.get("label") or "").strip()
        mlabel = (s.get("recordLabel") or "").strip()
        if slabel:
            if not mlabel:
                issues.append({"field": "recordLabel", "kind": "MISSING", "spotify": slabel})
                patch["recordLabel"] = slabel
                stats["label_missing_filled"] += 1
            elif not sp.album_same(mlabel, slabel):
                issues.append({"field": "recordLabel", "kind": "MISMATCH",
                               "app": mlabel, "spotify": slabel})
                stats["label_mismatch"] += 1

        # ── cover art (fill only; never replace a working cover) ───────────
        if not (s.get("coverArt") or "").strip() and hit.get("images"):
            patch["coverArt"] = hit["images"][0]
            issues.append({"field": "coverArt", "kind": "MISSING", "spotify": hit["images"][0]})
            stats["cover_missing_filled"] += 1

        # ── album name sanity (report only) ────────────────────────────────
        existing_fmt = formats.get(sid)
        salbum = hit.get("album") or ""
        if existing_fmt and existing_fmt.startswith("Album: "):
            if salbum and not sp.album_same(existing_fmt[7:], salbum):
                issues.append({"field": "album", "kind": "MISMATCH",
                               "app": existing_fmt[7:], "spotify": salbum})
                stats["album_mismatch"] += 1
        elif not existing_fmt and salbum:
            atype = hit.get("album_type") or alb.get("album_type")
            fmt = "Single" if atype == "single" else f"Album: {salbum}"
            issues.append({"field": "releaseFormat", "kind": "MISSING", "spotify": fmt})
            formats[sid] = fmt
            stats["format_missing_filled"] += 1

        if issues:
            findings.append({"id": sid, "title": title, "artist": artist,
                             "spotify_id": hit.get("id"), "issues": issues})

        if args.apply and patch:
            s.update(patch)
            tmp = fp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=1)
            os.replace(tmp, fp)
            changed_files += 1

        if i % 25 == 0:
            _tick(i, len(files), t0, stats)

    sp.flush_cache()

    if args.apply:
        tmp = RELEASE_FORMATS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(formats, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, RELEASE_FORMATS)

    report = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
              "applied": args.apply, "files_written": changed_files,
              "stats": stats, "findings": findings}
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    _summary(stats, args.apply, changed_files, time.time() - t0)


def _tick(i, n, t0, stats):
    el = time.time() - t0
    rate = (i + 1) / max(el, 0.001)
    eta = (n - i - 1) / max(rate, 0.001)
    line = (f"[{i+1}/{n}] matched={stats['matched']} nomatch={stats['nomatch']} "
            f"yearMiss={stats['year_missing_filled']} yearBad={stats['year_mismatch']} "
            f"labelMiss={stats['label_missing_filled']} "
            f"eta={eta/60:.1f}min")
    print(line, flush=True)
    try:
        with open(PROGRESS, "w") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _summary(st, applied, changed, secs):
    m = max(1, st["matched"])
    print("\n" + "=" * 62)
    print(f"SPOTIFY SWEEP {'(APPLIED)' if applied else '(REPORT ONLY)'}  {secs/60:.1f} min")
    print("=" * 62)
    print(f"songs swept        : {st['total']}")
    print(f"  matched          : {st['matched']}  ({100*st['matched']/max(1,st['total']):.1f}%)")
    print(f"  no Spotify match : {st['nomatch']}  ({100*st['nomatch']/max(1,st['total']):.1f}%)")
    print(f"  API errors       : {st['api_errors']}  (not counted as errors — retry to resolve)")
    print("\nERRORS FOUND (of matched songs):")
    print(f"  year wrong (>1y) : {st['year_mismatch']:5d}  ({100*st['year_mismatch']/m:.1f}%)   <- app dated AFTER release")
    print(f"  year app-older   : {st['year_app_older']:5d}  ({100*st['year_app_older']/m:.1f}%)   <- report only, Spotify may lack original press")
    print(f"  label wrong      : {st['label_mismatch']:5d}  ({100*st['label_mismatch']/m:.1f}%)")
    print(f"  album wrong      : {st['album_mismatch']:5d}  ({100*st['album_mismatch']/m:.1f}%)")
    print("\nGAPS FILLABLE FROM SPOTIFY:")
    print(f"  year missing     : {st['year_missing_filled']:5d}")
    print(f"  label missing    : {st['label_missing_filled']:5d}")
    print(f"  cover missing    : {st['cover_missing_filled']:5d}")
    print(f"  format missing   : {st['format_missing_filled']:5d}")
    if applied:
        print(f"\nsong files written : {changed}")
    print(f"\nreport → {REPORT}")


if __name__ == "__main__":
    main()
