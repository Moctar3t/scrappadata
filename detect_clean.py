#!/usr/bin/env python3
"""
detect_clean.py — find catalogue songs whose audio is the CENSORED (radio/clean)
edit rather than the explicit master.

WHY THE EXISTING GATE CANNOT CATCH THESE
  A clean edit does not shorten the track: the profanity is muted, reversed or
  pitched in place. Runtime is identical to the explicit master, so duration
  matching — the whole basis of the source gate — is blind to it by construction.
  It has to be heard.

THE SIGNATURE
  We publish isolated stems, which makes this measurable. In a censored edit the
  VOCAL drops to near-silence for a fraction of a second while the BEAT continues
  at full level, with loud vocal either side. A natural phrase break looks
  different: the vocal gap is longer, and it usually coincides with an
  arrangement change rather than sitting mid-phrase over an unbroken beat.

  So we count windows where ALL of:
    · vocal RMS falls below CUT x its own median          (the mute)
    · the gap lasts 0.08 - 0.60 s                          (a word, not a bar)
    · vocal is above its median within 0.35 s either side  (mid-phrase)
    · instrumental stays above 0.55 x its median           (beat keeps going)

  Reported as a count, not a verdict: a high count means "listen to this one".
"""
import json, subprocess, sys, time, urllib.request
from pathlib import Path

CDN = "https://dimesystemsstorage.b-cdn.net"
STEMS = ["bass", "drums", "other", "vocals"]
WORK = Path("/tmp/clean_work")
SR = 22050
CUT = 0.06          # fraction of median vocal RMS that counts as "muted"
MIN_S, MAX_S = 0.08, 0.60
GUARD_S = 0.35
INSTR_FLOOR = 0.55

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
    import numpy as np, librosa, warnings
    warnings.filterwarnings("ignore")
    WORK.mkdir(parents=True, exist_ok=True)
    sig = {}
    for name, url in stem_urls.items():
        p = WORK / f"{name}.m4a"
        if not fetch(url, p):
            return None, f"download failed: {name}"
        y, _ = librosa.load(str(p), sr=SR, mono=True)
        sig[name] = y
    n = min(len(v) for v in sig.values())
    if n < SR * 20:
        return None, "audio too short"
    voc = sig["vocals"][:n]
    instr = sum(sig[k][:n] for k in ("bass", "drums", "other"))

    hop = 256
    rv = librosa.feature.rms(y=voc, frame_length=1024, hop_length=hop)[0]
    ri = librosa.feature.rms(y=instr, frame_length=1024, hop_length=hop)[0]
    fps = SR / hop
    # medians over frames where the stem is actually present
    mv = float(np.median(rv[rv > rv.max() * 0.02])) if rv.max() > 0 else 0.0
    mi = float(np.median(ri[ri > ri.max() * 0.02])) if ri.max() > 0 else 0.0
    if mv <= 0 or mi <= 0:
        return None, "silent stem"

    muted = rv < (mv * CUT)
    events, i, N = [], 0, len(muted)
    gmin, gmax = int(MIN_S * fps), int(MAX_S * fps)
    guard = int(GUARD_S * fps)
    while i < N:
        if not muted[i]:
            i += 1; continue
        j = i
        while j < N and muted[j]:
            j += 1
        length = j - i
        if gmin <= length <= gmax:
            a0, a1 = max(0, i - guard), i
            b0, b1 = j, min(N, j + guard)
            before = rv[a0:a1].max() if a1 > a0 else 0
            after = rv[b0:b1].max() if b1 > b0 else 0
            instr_here = ri[i:j].mean() if j > i else 0
            if before > mv and after > mv and instr_here > mi * INSTR_FLOOR:
                a = voc[int(i * hop):int(j * hop)]
                peak = float(np.abs(a).max()) if a.size else 0.0
                depth = peak / (float(np.abs(voc).max()) or 1.0)
                events.append({"t": round(i / fps, 2), "dur": round(length / fps, 2),
                               "depth": round(depth, 5)})
        i = j
    hard = [e for e in events if e["depth"] < 0.002]
    return {"events": len(events), "hard": len(hard),
            "at": [e["t"] for e in hard[:12]] or [e["t"] for e in events[:12]],
            "median_depth": round(float(np.median([e["depth"] for e in events])), 5) if events else None,
            "duration": round(n / SR, 1)}, ""

def main():
    targets = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])
    done = set()
    if out_path.exists():
        for ln in out_path.read_text(encoding="utf-8").splitlines():
            try: done.add(json.loads(ln)["id"])
            except Exception: pass
    todo = [t for t in targets if t["id"] not in done]
    print(f"[clean] {len(todo)} to analyse", flush=True)
    for i, t in enumerate(todo, 1):
        for f in WORK.glob("*"):
            try: f.unlink()
            except Exception: pass
        res, err = analyse(t["stems"])
        rec = {"id": t["id"], "artist": t["artist"], "title": t["title"]}
        rec.update({"error": err} if res is None else res)
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        mark = f"{rec.get('events','-'):>3} cuts" if res else "ERR"
        print(f"[clean] {i}/{len(todo)} {mark}  {t['artist'][:22]} — {t['title'][:30]}", flush=True)
    print("[clean] done", flush=True)

if __name__ == "__main__":
    main()
