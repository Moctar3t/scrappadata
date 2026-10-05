#!/usr/bin/env python3
"""Apply the CONFIRMED corrections from enrichment_audit.json.

Scope — only what the dry run actually verified, nothing heuristic:
  1. junk credits ("ok", "-", "None", ...)         -> cleared
  2. year, 2+ independent sources agree            -> corrected
  3. recordLabel, 2+ independent sources agree     -> corrected
  4. album name wrong (existing Album:/Ep: entry)  -> name replaced, kind kept
  5. single misfiled (belongs to an album/EP)      -> reclassified using the
                                                        source's own release_type

NOT touched here (need audio re-verification, not text consensus):
  BPM half/double-time suspects, key, cover art. Those are a separate pass.

Every file this script is about to write is backed up first into
enrichment_fix_backup_<timestamp>/, and every change is logged to
enrichment_fix_log.json so it can be diffed or reverted.

Usage:
    python3 apply_enrichment_fixes.py --dry-run     # print the diff, write nothing
    python3 apply_enrichment_fixes.py                # apply
"""
from __future__ import annotations
import argparse, json, os, re, shutil, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meta_sources as ms

APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
SONGS = os.path.join(APP, "songs")
FORMATS = os.path.join(APP, "release-formats.json")
AUDIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enrichment_audit.json")
BACKUP_ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BACKUP_ROOT, "enrichment_fix_log.json")

JUNK = {"none", "null", "nan", "n/a", "na", "-", "--", "?", "unknown", "unknown artist",
        "ok", "okay", "tbd", "todo", "various", "various artists", "0", "undefined"}


def load(p, d=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return d


def is_junk(v) -> bool:
    return isinstance(v, str) and v.strip().lower() in JUNK


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    audit = load(AUDIT)
    if not audit:
        sys.exit("run audit_enrichment.py first")
    formats = load(FORMATS, {}) or {}
    changes = {"junk_cleared": [], "year": [], "label": [],
              "album_renamed": [], "single_reclassified": []}

    bdir = None
    backed_up = set()
    if not a.dry_run:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        bdir = os.path.join(BACKUP_ROOT, f"enrichment_fix_backup_{stamp}")
        os.makedirs(os.path.join(bdir, "songs"), exist_ok=True)
        shutil.copy2(FORMATS, os.path.join(bdir, "release-formats.json"))
        print(f"[backup] -> {bdir}")

    def backup_song(fp):
        if bdir and fp not in backed_up and os.path.exists(fp):
            shutil.copy2(fp, os.path.join(bdir, "songs", os.path.basename(fp)))
            backed_up.add(fp)

    # ── 1. junk credits ──────────────────────────────────────────────────────
    for field, code in (("producers", "producers.junk"), ("recordLabel", "recordLabel.junk")):
        for sid in audit["structural"]["problems"].get(code, []):
            fp = os.path.join(SONGS, f"{int(sid):04d}.json")
            s = load(fp)
            if not s:
                continue
            old = s.get(field)
            if is_junk(old):
                changes["junk_cleared"].append({"id": sid, "field": field, "old": old})
                s[field] = ""
                if not a.dry_run:
                    backup_song(fp)
                    json.dump(s, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ── 2 & 3. year / label from consensus ──────────────────────────────────
    for f in audit["consensus"]["findings"]:
        sid = f["id"]
        fp = os.path.join(SONGS, f"{int(sid):04d}.json")
        s = load(fp)
        if not s:
            continue
        touched = False
        for issue in f["issues"]:
            if issue["field"] == "year":
                changes["year"].append({"id": sid, "title": f["title"], "artist": f["artist"],
                                        "old": s.get("year"), "new": issue["suggested"],
                                        "sources": issue["sources"]})
                s["year"] = str(issue["suggested"])
                touched = True
            elif issue["field"] == "recordLabel":
                changes["label"].append({"id": sid, "title": f["title"], "artist": f["artist"],
                                         "old": s.get("recordLabel"), "new": issue["suggested"],
                                         "sources": issue["sources"]})
                s["recordLabel"] = issue["suggested"]
                touched = True
        if touched and not a.dry_run:
            backup_song(fp)
            json.dump(s, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ── 4 & 5. album ─────────────────────────────────────────────────────────
    for f in audit["consensus"]["findings"]:
        sid = f["id"]
        for issue in f["issues"]:
            if issue["field"] != "album":
                continue
            fp = os.path.join(SONGS, f"{int(sid):04d}.json")
            s = load(fp) or {}
            if issue["app"] == "Single":
                # Reclassify using the SOURCE's own release_type — a "single" that
                # is structurally an EP release must become "Ep: ", not "Album: ".
                is_ep = False
                dk = f"deezer|{ms.norm(s.get('artist',''))}|{ms.norm(s.get('title',''))}"
                d = ms._load_cache().get(dk) or {}
                if (d.get("release_type") or "").lower() == "ep":
                    is_ep = True
                new_val = f"{'Ep' if is_ep else 'Album'}: {issue['suggested']}"
                changes["single_reclassified"].append({
                    "id": sid, "title": f["title"], "artist": f["artist"],
                    "old": "Single", "new": new_val, "sources": issue["sources"]})
                formats[sid] = new_val
            else:
                prefix = "Ep: " if str(formats.get(sid, "")).startswith(("Ep: ", "EP: ")) else "Album: "
                new_val = prefix + issue["suggested"]
                changes["album_renamed"].append({
                    "id": sid, "title": f["title"], "artist": f["artist"],
                    "old": formats.get(sid), "new": new_val, "sources": issue["sources"]})
                formats[sid] = new_val

    if not a.dry_run:
        json.dump(formats, open(FORMATS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    total = sum(len(v) for v in changes.values())
    print(f"junk credits cleared     : {len(changes['junk_cleared'])}")
    print(f"years corrected          : {len(changes['year'])}")
    print(f"labels corrected         : {len(changes['label'])}")
    print(f"album names corrected    : {len(changes['album_renamed'])}")
    print(f"singles reclassified     : {len(changes['single_reclassified'])}")
    print(f"TOTAL edits               : {total}")

    if a.dry_run:
        print("\n(dry run — nothing written)")
        return

    json.dump({"applied": time.strftime("%Y-%m-%d %H:%M:%S"), "changes": changes},
              open(LOG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nchange log -> {LOG}")


if __name__ == "__main__":
    main()
