#!/usr/bin/env python3
"""
vps_vibe.py: La boîte bleue on the VPS: the six vibe numbers (valence,
danceability, energy, acousticness, speechiness, instrumentalness) of catalogue
songs, from their four stems on the CDN.

Why it is needed: an Exportify CSV carries Spotify's numbers, but Spotify closed
its audio-features endpoint to new apps in Nov 2024, so a song added from the
Spotify playlist, an album or a track lookup arrives with none. The app's AUTO
taste, the visualiser's mood and the Oracle all read them.

Runs ON THE VPS, where the model already lives (/var/www/zingy-api/analysis/lbb,
the same one that scores listeners' uploads), with the stems one CDN fetch away.
It uses the stems path, the model's best (mean test R² 0.49).

Usage: vps_vibe.py targets.json out.jsonl
  targets: [{"id": "6407", "stems": {"bass": url, "drums": url, "other": url, "vocals": url}}]
  out:     one line per target, {"id", <the six numbers>} or {"id", "error"}
An id already in out.jsonl is not scored again, so a cut run resumes.
"""
import json, os, shutil, sys, tempfile, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LBB = Path("/var/www/zingy-api/analysis/lbb")
STEMS = ("vocals", "drums", "bass", "other")

def fetch(url: str, dest: Path, tries: int = 3) -> bool:
    for a in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            if dest.stat().st_size > 10_000:
                return True
        except Exception:
            pass
        time.sleep(1.5 * (a + 1))
    return False

def main() -> None:
    # The live stream's encoder runs on this machine: it always comes first.
    os.nice(15)
    import warnings
    warnings.filterwarnings("ignore")   # librosa's audioread notices, once per stem
    sys.path.insert(0, str(LBB))
    import numpy as np
    import lightgbm as lgb
    import features

    cfg = json.loads((LBB / "config.json").read_text(encoding="utf-8"))
    boosters = {t: lgb.Booster(model_str=(LBB / "models" / f"lgbm_{t}.txt").read_text(encoding="utf-8"))
                for t in cfg["targets"]}
    targets = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    done = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(str(json.loads(line)["id"]))

    with open(out, "a", encoding="utf-8") as f:
        for i, t in enumerate(targets, 1):
            cid = str(t["id"])
            if cid in done:
                continue
            tmp = Path(tempfile.mkdtemp(prefix="vibe_"))
            try:
                paths = {s: tmp / f"{s}{Path(t['stems'][s]).suffix or '.m4a'}" for s in STEMS}
                with ThreadPoolExecutor(max_workers=4) as tp:
                    ok = list(tp.map(lambda s: fetch(t["stems"][s], paths[s]), STEMS))
                if not all(ok):
                    rec = {"id": cid, "error": "stem download failed: "
                           + ",".join(s for s, k in zip(STEMS, ok) if not k)}
                else:
                    feats = features.extract_from_paths(paths)
                    x = np.array([[feats.get(k, np.nan) for k in cfg["feature_names"]]])
                    rec = {"id": cid}
                    for name, booster in boosters.items():
                        z = float(booster.predict(x)[0])
                        v = 1.0 / (1.0 + np.exp(-z)) if name in cfg["logit_targets"] else float(np.clip(z, 0.0, 1.0))
                        rec[name] = round(float(v), 4)
            except Exception as e:
                rec = {"id": cid, "error": f"{type(e).__name__}: {e}"[:300]}
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            f.write(json.dumps(rec) + "\n")
            f.flush()
            print(f"[{i}/{len(targets)}] {cid} {rec.get('error', 'ok')}", flush=True)

if __name__ == "__main__":
    main()
