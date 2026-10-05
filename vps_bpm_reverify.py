#!/usr/bin/env python3
"""Re-verify the 301 BPM half/double-time SUSPECTS against the actual audio.

The catalogue audit flagged these on text alone (values <78 or >185, or sitting
on a semitone-quantised grid) — that is suspicion, not proof. A real slow ballad
at 68 BPM would get flagged too, and blindly doubling it would corrupt correct
data. This script settles it against the real stem audio: for each candidate
tempo AND its octave-neighbours (half, double), score how well the onset
envelope's autocorrelation actually supports that periodicity, and only change
the value when the audio itself prefers a different octave.

Same method as the GPU worker's _tempo_octave_fix (worker_gpu.py) — this is that
function's logic applied retroactively to the existing catalogue instead of only
new ingests, run here on the VPS (4 CPU cores, no GPU needed — this is pure
signal analysis, not separation) so it doesn't compete with the GPU worker or
saturate a home connection downloading 301 stems.

Input:  /root/bpm_manifest.json  [{id, artist, title, bpm, url}, ...]
Output: /root/bpm_results.json   [{id, old_bpm, detected_bpm, confidence, ...}]
"""
from __future__ import annotations
import concurrent.futures as cf
import json
import os
import tempfile
import time
import warnings

import numpy as np
import requests
import librosa

MANIFEST = "/root/bpm_manifest.json"
RESULTS = "/root/bpm_results.json"
WORKERS = 4


def onset_autocorr(y: np.ndarray, sr: int):
    """One onset-strength autocorrelation per song, reused for every candidate so
    every comparison in analyze_one() is apples-to-apples."""
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    hop = 512
    ac = librosa.autocorrelate(onset, max_size=len(onset))
    if ac.size == 0 or ac[0] <= 0:
        return None, hop
    return ac / ac[0], hop


def strength_at(ac, hop: int, sr: int, cand: float) -> float:
    if ac is None or cand <= 0:
        return -1.0
    lag = (60.0 / cand) * sr / hop
    i = int(round(lag))
    if i < 1 or i >= len(ac):
        return -1.0
    return float(np.max(ac[max(1, i - 1): i + 2]))


def analyze_one(job: dict) -> dict:
    sid, url, old_bpm = job["id"], job["url"], job["bpm"]
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
            f.write(r.content)
            path = f.name
        warnings.filterwarnings("ignore", category=UserWarning)
        # 90s is plenty to lock a tempo and keeps this fast across 301 files
        y, sr = librosa.load(path, sr=22050, duration=90)
        os.unlink(path)

        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        raw = float(tempo[0]) if isinstance(tempo, np.ndarray) else float(tempo)

        # The question is ONLY "is old_bpm's own octave family right, half, or
        # double" — never "what's the single best tempo anywhere in range".
        # Candidates MUST be old_bpm's own {x1, x2, x0.5}, scored against ONE
        # shared autocorrelation. Anchoring on the raw librosa estimate instead
        # (the first version of this script) let the search drift onto an
        # unrelated sub-harmonic — e.g. old_bpm=201 (Eric Clapton, correct),
        # raw=99.4 (~201/2, itself confirming 201), but a further half at 49.7
        # scored higher on an autocorrelation artifact and got reported as the
        # "fix" — a value with no real relationship to 201. 51 of 78 "confirmed"
        # changes on the first run were exactly this: a non-octave ratio to
        # old_bpm. Anchoring here makes that failure mode structurally impossible
        # — detected_bpm is always exactly old_bpm, old_bpm*2, or old_bpm/2.
        ac, hop = onset_autocorr(y, sr)
        family = {}
        for cand in (float(old_bpm), float(old_bpm) * 2, float(old_bpm) / 2):
            if 40 <= cand <= 230:
                family[round(cand, 1)] = strength_at(ac, hop, sr, cand)

        best_cand = max(family, key=family.get)
        best_strength = family[best_cand]
        old_strength = family[round(float(old_bpm), 1)]

        detected = round(best_cand)
        # Only worth changing when the preferred octave clearly beats the
        # currently-stored one — not a coin flip.
        confident = best_strength > old_strength * 1.08 and detected != round(float(old_bpm))

        return {"id": sid, "artist": job["artist"], "title": job["title"],
                "old_bpm": old_bpm, "raw_librosa": round(raw, 1),
                "detected_bpm": detected, "scores": family,
                "confident_change": confident, "error": None}
    except Exception as e:
        return {"id": sid, "artist": job.get("artist"), "title": job.get("title"),
                "old_bpm": old_bpm, "error": str(e)[:200]}


def main():
    jobs = json.load(open(MANIFEST))
    print(f"[bpm] {len(jobs)} songs to re-verify, {WORKERS} workers")
    results = []
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(analyze_one, j): j for j in jobs}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            res = fut.result()
            results.append(res)
            if i % 20 == 0 or i == len(jobs):
                el = time.time() - t0
                changed = sum(1 for r in results if r.get("confident_change"))
                errs = sum(1 for r in results if r.get("error"))
                print(f"  [{i}/{len(jobs)}] {el:.0f}s  confirmed-changes={changed} errors={errs}",
                     flush=True)
            json.dump(results, open(RESULTS, "w"))
    changed = sum(1 for r in results if r.get("confident_change"))
    errs = sum(1 for r in results if r.get("error"))
    print(f"\nDONE. {len(results)} analyzed, {changed} confirmed octave changes, "
          f"{errs} errors -> {RESULTS}")


if __name__ == "__main__":
    main()
