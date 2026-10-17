#!/usr/bin/env python3
"""
replicate_line.py - the Mac side of the Replicate line (no home GPU).

The ingest has two lines for the same stages:
  gpu        the GPU box over SSH (gpu_agent.py): search, fetch, demucs, encode
  replicate  the VPS (vps_line.py): search, fetch, split on Replicate, encode

A track's line is set when it enters the manifest (`prepare --line`), and the
Spotify inbox uses the replicate line. Everything after the stems are on the
Mac (upload, Genius, La boîte bleue, publish) is the same for both.

YouTube refuses the VPS's address, so for the length of a stage the Mac opens
a SOCKS tunnel out of its own connection (`ssh -R`), bound to the VPS's
loopback only, and every YouTube request goes through it. IPv4 only: YouTube
signs each audio link for the address that asked for it, and a link asked for
over IPv6 then fetched over IPv4 is a 403.
"""
from __future__ import annotations
import base64, json, os, random, socket, subprocess, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VPS = "root@" + os.environ.get("ZINGY_VPS", "204.168.236.140")
REMOTE_DIR = "/root/zingy_line"
PY = "/var/www/zingy-api/analysis/venv/bin/python"   # has librosa for BPM/key
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15"]

class Tunnel:
    """`with Tunnel() as port:` the Mac's connection, as a SOCKS proxy on the VPS."""
    def __enter__(self) -> int:
        for _ in range(5):
            self.port = random.randint(20000, 40000)
            self.proc = subprocess.Popen(SSH + ["-4", "-N", "-o", "ExitOnForwardFailure=yes",
                                                "-R", f"127.0.0.1:{self.port}", VPS],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for _ in range(30):
                time.sleep(0.5)
                if self.proc.poll() is not None:
                    break
                rc = subprocess.run(SSH + [VPS, f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 8 "
                                                f"-x socks5h://127.0.0.1:{self.port} https://www.youtube.com/generate_204"],
                                    capture_output=True, text=True, timeout=30).stdout.strip()
                if rc == "204":
                    return self.port
            self.proc.kill()
        raise RuntimeError("could not open the tunnel from the Mac to the VPS")

    def __exit__(self, *a):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

def push() -> None:
    """The current worker scripts on the VPS, every stage: never a stale copy."""
    subprocess.run(SSH + [VPS, f"mkdir -p {REMOTE_DIR}"], check=True, timeout=60)
    subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                    str(HERE / "vps_line.py"), str(HERE / "vps_analyze.py"), f"{VPS}:{REMOTE_DIR}/"],
                   check=True, timeout=120)

def call(command: str, args: dict, timeout: int) -> dict:
    blob = base64.b64encode(json.dumps(args, ensure_ascii=False).encode()).decode()
    try:
        r = subprocess.run(SSH + [VPS, f"{PY} {REMOTE_DIR}/vps_line.py {command} {blob}"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"vps {command}: timed out after {timeout}s", "unavailable": True}
    for line in (r.stdout or "").splitlines():
        if line.startswith("__LINE__"):
            return json.loads(line[len("__LINE__"):])
    return {"ok": False, "error": f"vps {command}: {((r.stderr or r.stdout) or '').strip()[-300:]}",
            "unavailable": r.returncode == 255}

def info(url: str, port: int, playlist: bool = False) -> dict:
    """A video's metadata, or a playlist's ids (link_intake.py)."""
    return call("info", {"url": url, "proxy": port, "playlist": playlist, "timeout": 150}, 200)

def search(query: str, port: int, limit: int = 12) -> dict:
    return call("search", {"query": query, "limit": limit, "proxy": port, "timeout": 150}, 200)

def produce(key: str, urls: list[str], want_s: float, port: int, *, analyze: bool,
            dest: Path, tolerance: float, bitrate: str) -> dict:
    """Fetch, check, split, encode, measure on the VPS; then the four m4a here
    in `dest`, and the VPS's copy gone."""
    r = call("produce", {"key": key, "urls": urls, "want_s": want_s, "proxy": port,
                         "analyze": analyze, "tolerance": tolerance, "bitrate": bitrate}, 3000)
    if not r.get("ok"):
        return r
    dest.mkdir(parents=True, exist_ok=True)
    srcs = [f"{VPS}:{f['path']}" for f in r["files"].values()]
    p = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"] + srcs + [str(dest)],
                       capture_output=True, text=True, timeout=600)
    short = [s for s, f in r["files"].items()
             if not (dest / f"{s}.m4a").exists() or (dest / f"{s}.m4a").stat().st_size != f["size"]]
    call("cleanup", {"keys": [key]}, 60)
    if short:
        return {"ok": False, "error": f"pull failed for {short}: {(p.stderr or '').strip()[-160:]}"}
    return r
