#!/usr/bin/env python3
"""Apply the CONFIRMED BPM octave corrections from bpm_results_drums_fixed.json.

Every entry with confident_change=true has, by construction, a genuine 2x or
0.5x relationship to the stored value (verified again here before writing
anything) — scored against the drums stem specifically (the correct signal for
onset/tempo tracking) via autocorrelation on the VPS's real audio, not text
heuristics. See vps_bpm_reverify.py for the method and the two false starts
(wrong stem, then wrong candidate-anchoring) that preceded this run.

Backs up every touched song file first, same pattern as apply_enrichment_fixes.py.
"""
from __future__ import annotations
import glob, json, os, shutil, sys, time

APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
SONGS = os.path.join(APP, "songs")
RESULTS = "/Users/dime/Developer/scrappadata/bpm_results_drums_fixed.json"
BACKUP_ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BACKUP_ROOT, "bpm_fix_log.json")


def main():
    results = json.load(open(RESULTS))
    changes = [r for r in results if r.get("confident_change")]

    # Re-verify the octave guarantee independently, right before writing files —
    # never trust "it should be fine by construction" at the point of mutation.
    bad = []
    for r in changes:
        ratio = r["detected_bpm"] / r["old_bpm"] if r["old_bpm"] else 0
        if not (abs(ratio - 2) < 0.15 or abs(ratio - 0.5) < 0.08):
            bad.append(r)
    if bad:
        sys.exit(f"REFUSING TO APPLY: {len(bad)} entries are not genuine octave "
                 f"relationships (e.g. {bad[0]['id']}: {bad[0]['old_bpm']} -> "
                 f"{bad[0]['detected_bpm']}). Fix the source data before rerunning.")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    bdir = os.path.join(BACKUP_ROOT, f"bpm_fix_backup_{stamp}")
    os.makedirs(os.path.join(bdir, "songs"), exist_ok=True)
    print(f"[backup] -> {bdir}")

    applied = []
    for r in changes:
        fp = os.path.join(SONGS, f"{int(r['id']):04d}.json")
        s = json.load(open(fp, encoding="utf-8"))
        if s.get("bpm") != r["old_bpm"]:
            print(f"  [skip] id={r['id']} — stored bpm {s.get('bpm')} no longer "
                 f"matches what was analyzed ({r['old_bpm']}); left untouched")
            continue
        shutil.copy2(fp, os.path.join(bdir, "songs", os.path.basename(fp)))
        s["bpm"] = r["detected_bpm"]
        json.dump(s, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        applied.append({"id": r["id"], "artist": r["artist"], "title": r["title"],
                        "old_bpm": r["old_bpm"], "new_bpm": r["detected_bpm"]})

    json.dump({"applied": time.strftime("%Y-%m-%d %H:%M:%S"),
              "method": "drums-stem autocorrelation, old_bpm-anchored octave family",
              "changes": applied},
             open(LOG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"\nBPM corrections applied: {len(applied)} / {len(changes)} candidates")
    print(f"change log -> {LOG}")


if __name__ == "__main__":
    main()
