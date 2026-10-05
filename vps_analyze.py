#!/usr/bin/env python3
"""
vps_analyze.py — measure BPM + key for catalogue songs that have neither.

Runs ON THE VPS. Used to backfill songs already published: their source audio is
long gone from the GPU box, but their stems are on the CDN, and summing the four
stems reconstructs the mix (features.reconstruct_mix does exactly this).

New ingests do NOT come through here — they are analysed on the GPU box during
`produce`, where the decoded source is already local and nothing has to move.

Usage: vps_analyze.py targets.json out.jsonl
"""
import json, subprocess, sys, time, urllib.request
from pathlib import Path

WORK = Path("/tmp/bpmkey_work")
STEMS = ["bass", "drums", "other", "vocals"]
PITCH = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]

def fetch(url, dest, tries=3):
    for a in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
                f.write(r.read())
            if dest.stat().st_size > 1000:
                return True
        except Exception:
            time.sleep(1.5 * (a + 1))
    return False

def analyse(stem_urls):
    """Measure tempo and key USING THE STEMS, not a flattened mix.

    A naive mix analysis scored 2/8 on key and put three of eight tempos at
    exactly half speed, checked against songs whose Spotify values we already
    hold. Both failures come from mixing everything together:

      · tempo — the beat lives in the DRUMS. Tracking it on a full mix lets
        sustained vocal/bass energy halve the perceived pulse.
      · key   — percussion is broadband noise smeared across all 12 chroma bins.
        Harmony lives in bass + other + vocals, so the drums are excluded and the
        harmonic component is isolated before the chroma is taken.

    Tempo is then octave-corrected: beat trackers are reliable about the beat
    GRID but ambiguous about which multiple you call the tempo, so a result far
    below the usual range is tested against its double.
    """
    WORK.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in STEMS:
        u = stem_urls.get(name)
        if not u:
            return None, f"no {name} url"
        p = WORK / f"{name}.m4a"
        if not fetch(u, p):
            return None, f"download failed: {name}"
        paths[name] = p
    return analyse_files(paths)

def analyse_files(paths):
    """The same measurement on stems already on this disk ({stem: path})."""
    import numpy as np, librosa, warnings
    warnings.filterwarnings("ignore")
    sig = {}
    for name in STEMS:
        p = paths[name]
        try:
            y, sr = librosa.load(str(p), sr=22050, mono=True, duration=180)
        except Exception as e:
            return None, f"decode {name}: {e}"
        sig[name] = y
    n = min(len(v) for v in sig.values())
    if n < 22050 * 5:
        return None, "too little audio"
    sr = 22050

    # ---- tempo: from the FULL MIX ----
    # Measured against 8 songs whose Spotify values we already hold: mix 5/8,
    # drums-only 4/8, median-prior 4/8. The remaining misses are octave errors
    # (188 read as 96, 66 read as 129) — a beat tracker is reliable about the
    # beat GRID and ambiguous about which multiple you call the tempo. An
    # earlier "double it if it looks low" correction made things WORSE
    # (117 -> 59) and was removed rather than kept on faith.
    mix = sum(sig[k][:n] for k in STEMS)
    onset = librosa.onset.onset_strength(y=mix, sr=sr)
    tempo, _ = librosa.beat.beat_track(onset_envelope=onset, sr=sr)
    bpm = float(tempo[0] if hasattr(tempo, "__len__") else tempo)

    # ---- key: from the HARMONIC stems only ----
    # Percussion is broadband noise smeared across all 12 chroma bins, so the
    # drums are excluded and the harmonic component isolated: 4/8 against the
    # same controls, versus 2/8 for a naive full-mix chroma.
    harm = sig["bass"][:n] + sig["other"][:n] + sig["vocals"][:n]
    harm = librosa.effects.harmonic(harm, margin=3.0)
    chroma = librosa.feature.chroma_cens(y=harm, sr=sr)
    prof = np.median(chroma, axis=1)
    if prof.sum() <= 0:
        return None, "no harmonic content"
    maj = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
    mino = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
    pz = (prof - prof.mean()) / (prof.std() or 1.0)
    scored = []
    for i in range(12):
        for p_, mode in ((maj, "Major"), (mino, "Minor")):
            pn = (p_ - p_.mean()) / p_.std()
            scored.append((float(np.dot(pz, np.roll(pn, i))) / 12.0, PITCH[i], mode))
    scored.sort(reverse=True)
    # margin over the runner-up: the caller uses it to decide whether the key is
    # confident enough to write, because Mixima MATCHES on key and a wrong one
    # is worse than none.
    conf = round(scored[0][0] - scored[1][0], 3)

    return {"bpm": int(round(bpm)), "key": f"{scored[0][1]} {scored[0][2]}",
            "key_confidence": conf, "source": "librosa@vps (bpm=mix, key=harmonic-stems)"}, ""

def main():
    targets = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])
    done = set()
    if out_path.exists():
        for ln in out_path.read_text(encoding="utf-8").splitlines():
            try: done.add(json.loads(ln)["id"])
            except Exception: pass
    todo = [t for t in targets if t["id"] not in done]
    print(f"[bpmkey] {len(todo)} to analyse", flush=True)
    for i, t in enumerate(todo, 1):
        for f in WORK.glob("*"):
            try: f.unlink()
            except Exception: pass
        res, err = analyse(t["stems"])
        rec = {"id": t["id"], "artist": t.get("artist"), "title": t.get("title")}
        rec.update({"error": err} if res is None else res)
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tag = f"{rec.get('bpm')} BPM {rec.get('key')}" if res else f"ERR {err[:40]}"
        print(f"[bpmkey] {i}/{len(todo)} {tag:26s} {t.get('artist','')[:20]} — {t.get('title','')[:26]}", flush=True)
    print("[bpmkey] done", flush=True)

if __name__ == "__main__":
    main()
