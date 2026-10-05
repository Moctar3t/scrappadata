#!/usr/bin/env python3
"""
apply_romaji.py — rewrite every Japanese-script song title in the catalogue to
romaji, using the plan produced by romaji.to_romaji().

Titles only. Artist names, stem URLs, ids and every other field are untouched,
so nothing about playback or identity changes — this is purely how the title
reads in the app.

Run `python3 apply_romaji.py` for a dry run, `--apply` to write and publish.
"""
from __future__ import annotations
import json, os, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from romaji import to_romaji, has_japanese  # noqa: E402

REPO = Path("/Users/dime/Developer/ios-zingy")
MUSICDATA = REPO / "App/ZingyApp/Resources/musicdata"
INDEX = MUSICDATA / "music-index.json"
SONGS = MUSICDATA / "songs"
CDN = "https://dimesystemsstorage.b-cdn.net"

def log(m): print(m, flush=True)

def polish(s: str) -> str:
    """iTunes sometimes returns an all-lowercase romanisation ('tasogare no bay
    city'). Title-case only in that case — never touch a title that already
    carries deliberate capitalisation."""
    if s and s == s.lower():
        return " ".join(w.capitalize() for w in s.split())
    return s

def main():
    apply = "--apply" in sys.argv
    cat = json.loads(INDEX.read_text(encoding="utf-8"))
    songs = cat["songsList"]
    plan_path = Path(__file__).with_name("romaji_plan.json")
    plan = {p["id"]: p for p in json.loads(plan_path.read_text(encoding="utf-8"))}

    changed, written, skipped = [], [], []
    for s in songs:
        p = plan.get(str(s["id"]))
        if not p:
            continue
        new = polish(p["new"])
        if not new or new == s["title"] or has_japanese(new):
            skipped.append((s["id"], s["title"], new)); continue
        log(f"  {s['id']:>5} {s['title'][:30]:30s} -> {new[:38]:38s} [{p['src']}]")
        changed.append((s["id"], s["title"], new, p["src"]))
        if apply:
            s["title"] = new

    log(f"\n{len(changed)} title(s) to romanise; {len(skipped)} skipped")
    for i, old, new in skipped:
        log(f"    skip {i}: {old} -> {new!r}")
    if not apply:
        log("\n(dry run — pass --apply to write and publish)")
        return

    # per-song JSON: legacy songs have no local file, so take the CDN copy as base
    for cid, old, new, _src in changed:
        fname = f"{int(cid):04d}.json" if str(cid).isdigit() else f"{cid}.json"
        local = SONGS / fname
        doc = None
        if local.exists():
            doc = json.loads(local.read_text(encoding="utf-8"))
        else:
            try:
                doc = json.loads(urllib.request.urlopen(
                    f"{CDN}/musicdata/songs/{fname}?v={int(time.time())}", timeout=30).read())
            except Exception:
                doc = None
        if doc is None:
            log(f"    ! no song JSON for {cid} — index updated, detail file left alone")
            continue
        doc["title"] = new
        local.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
        written.append(f"songs/{fname}")

    backup = INDEX.with_name(f"music-index.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    backup.write_bytes(INDEX.read_bytes())
    tmp = INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(cat, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, INDEX)
    log(f"\nbacked up -> {backup.name}; wrote {len(written)} song file(s)")

    fd, lp = tempfile.mkstemp(suffix=".txt"); os.close(fd)
    Path(lp).write_text("\n".join(written + ["music-index.json"]) + "\n", encoding="utf-8")
    r = subprocess.run(["bash", str(REPO / "scripts/upload-catalog.sh"), "--files-from", lp],
                       capture_output=True, text=True, timeout=1800)
    for line in (r.stdout or "").strip().splitlines()[-4:]:
        log("  " + line)

    live = json.loads(urllib.request.urlopen(
        f"{CDN}/musicdata/music-index.json?v={int(time.time())}", timeout=60).read().decode("utf-8", "replace"))
    still = [s for s in live["songsList"] if has_japanese(s.get("title", ""))]
    log(f"\nverified live: {len(still)} song(s) still have Japanese titles")
    for s in still[:6]:
        log(f"    {s['id']} {s['title']}")

if __name__ == "__main__":
    main()
