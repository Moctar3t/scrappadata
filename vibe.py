#!/usr/bin/env python3
"""
vibe.py: the Mac side of La boîte bleue: send songs to vps_vibe.py, get their
six vibe numbers back. One implementation, used by the ingest (`vibe` stage)
and by `zingy.py fix vibe` (songs already published).

The numbers are MEASURED by a model, the way BPM and key are measured when no
label data exists: never a default, and never written over a Spotify value.
"""
from __future__ import annotations
import json, os, subprocess, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VIBE = ("valence", "danceability", "energy", "acousticness", "speechiness", "instrumentalness")
VPS = "root@" + os.environ.get("ZINGY_VPS", "204.168.236.140")
REMOTE = "/root/vibe"
ON_VPS = os.environ.get("ZINGY_HOST") == "vps"
PY = "/var/www/zingy-api/analysis/venv/bin/python"

def log(m): print(m, flush=True)

def missing(song: dict) -> bool:
    """True when any of the six numbers is absent (an index entry or a CSV's features)."""
    return any(song.get(k) is None for k in VIBE)

def score_on_vps(targets: list[dict], tag: str) -> dict[str, dict]:
    """targets: [{"id", "stems": {bass, drums, other, vocals: url}}].
    Returns {id: {the six numbers} | {"error": ...}}. Resumable: a run cut short
    and started again with the same tag scores only what is left."""
    if not targets:
        return {}
    tf = HERE / f"vibe_{tag}_targets.json"
    tf.write_text(json.dumps(targets, ensure_ascii=False), encoding="utf-8")
    out = HERE / f"vibe_{tag}_out.jsonl"
    t0 = time.time()
    if ON_VPS:                       # the 24/7 ingest host: the model is right here
        log(f"  La boîte bleue: scoring {len(targets)} song(s) (~14 s each) …")
        subprocess.run([PY, str(HERE / "vps_vibe.py"), str(tf), str(out)], cwd=HERE,
                       timeout=max(900, 40 * len(targets)))
        return _read(out, len(targets), t0)
    ssh = ["ssh", "-o", "ConnectTimeout=20", VPS]
    subprocess.run(ssh + [f"mkdir -p {REMOTE}"], check=True, timeout=60)
    for f in (HERE / "vps_vibe.py", tf):
        subprocess.run(["scp", "-q", "-o", "ConnectTimeout=20", str(f), f"{VPS}:{REMOTE}/"],
                       check=True, timeout=300)
    log(f"  La boîte bleue: scoring {len(targets)} song(s) on the VPS (~14 s each) …")
    subprocess.run(ssh + [f"cd {REMOTE} && /var/www/zingy-api/analysis/venv/bin/python "
                          f"vps_vibe.py {tf.name} vibe_{tag}_out.jsonl"],
                   timeout=max(900, 40 * len(targets)))
    subprocess.run(["scp", "-q", "-o", "ConnectTimeout=20", f"{VPS}:{REMOTE}/vibe_{tag}_out.jsonl", str(out)],
                   check=True, timeout=300)
    return _read(out, len(targets), t0)

def _read(out: Path, n: int, t0: float) -> dict[str, dict]:
    got: dict[str, dict] = {}
    for line in out.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            got[str(r.pop("id"))] = r
    ok = sum(1 for r in got.values() if "error" not in r)
    log(f"  scored {ok}/{n} in {int(time.time() - t0)} s")
    return got

def finish(tag: str) -> None:
    """Forget a finished run, here and on the VPS, so the next one starts clean."""
    for p in (HERE / f"vibe_{tag}_targets.json", HERE / f"vibe_{tag}_out.jsonl"):
        p.unlink(missing_ok=True)
    if ON_VPS:
        return
    subprocess.run(["ssh", "-o", "ConnectTimeout=20", VPS,
                    f"rm -f {REMOTE}/vibe_{tag}_targets.json {REMOTE}/vibe_{tag}_out.jsonl"],
                   timeout=60)
