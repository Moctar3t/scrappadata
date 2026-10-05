#!/usr/bin/env python3
"""
fix_identities.py — correct three catalogue identity errors found 2026-08-30.

Each change below is backed by evidence gathered before editing anything; none
of it is guesswork, and nothing is deleted.

1. id 9 was titled "Plastic Love" but plays a DIFFERENT Mariya Takeuchi song.
   Evidence: tracks.original_url = youtube.com/watch?v=doLJtcxtxQ4, whose oEmbed
   title is "Namida no One-sided Love" on the official "Mariya Takeuchi - Topic"
   channel. The audio measures 290.8 s @ 68.0 BPM; real Plastic Love is
   294 s @ 103 BPM. So the audio is a genuine label master — just the wrong
   track. It is RETITLED, not deleted.
   Genius 8339374: 涙のワンサイデッド・ラヴ, 1979, album UNIVERSITY STREET.

2. Real Plastic Love was already split and sitting on the CDN at /Plastic Love/.
   Evidence: 294.36 s @ 103.4 BPM, and iTunes gives the VARIETY (1984) track as
   294 s — exact match. Waveform correlation against id 9's audio is 0.0002
   (different recordings). Added as a NEW song using those existing stems; no
   re-splitting needed.

3. id 77 (Yurie Kokubu — I wanna Be With You) carried the cover of DJ Khaled's
   "I Wanna Be With You (feat. Nicki Minaj)" from *Suffering From Success* — the
   classic iTunes limit=1 fuzzy-match failure. The correct release is Kokubu's
   STEPS +2 (2014), whose track runtime 306 s matches id 77 exactly.

bpm/key here are MEASURED from the audio (librosa beat-track +
Krumhansl-Schmuckler), same method the rest of the catalogue uses — they are
estimates, not label data, and are marked as such in the report.
"""
from __future__ import annotations
import json, os, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

REPO = Path("/Users/dime/Developer/ios-zingy")
MUSICDATA = REPO / "App/ZingyApp/Resources/musicdata"
INDEX = MUSICDATA / "music-index.json"
SONGS = MUSICDATA / "songs"
CDN = "https://dimesystemsstorage.b-cdn.net"
STEMS = ["bass", "drums", "other", "vocals"]

def log(m): print(m, flush=True)

def fetch_song_json(sid: str) -> dict | None:
    """Legacy songs have no local JSON — take the CDN copy as the base so we
    edit the live document rather than inventing a new one."""
    try:
        u = f"{CDN}/musicdata/songs/{sid}.json?v={int(time.time())}"
        return json.loads(urllib.request.urlopen(u, timeout=30).read())
    except Exception:
        return None

def mmss(s): return f"{int(s)//60}:{int(s)%60:02d}"

# ── the three corrections ───────────────────────────────────────────────────
NAMIDA_ART = "https://images.genius.com/b2f1d01e3c664dabadeb71c29dae19ff.1000x1000x1.jpg"
PLASTIC_ART = "https://images.genius.com/dbd97af0a34d5cfc23b30af8deccbf67.768x768x1.jpg"
PLASTIC_ART_FB = ("https://is1-ssl.mzstatic.com/image/thumb/Music125/v4/c9/8c/a6/"
                  "c98ca691-2006-6116-81f6-52ec22f5e25c/825646183807.jpg/1000x1000bb.jpg")
KOKUBU_ART = ("https://is1-ssl.mzstatic.com/image/thumb/Music124/v4/95/2a/0b/"
              "952a0b16-0dad-7993-4303-5b87dbc6c160/jacket_MHCL30221B00Z_550.jpg/1000x1000bb.jpg")

def main():
    apply = "--apply" in sys.argv
    cat = json.loads(INDEX.read_text(encoding="utf-8"))
    songs = cat["songsList"]
    by_id = {str(s.get("id")): s for s in songs}
    written: list[str] = []

    # ---------------------------------------------------------------- 1. id 9
    e = by_id["9"]
    log(f"[1] id 9 currently: {e['artist']} — {e['title']}  (bpm {e.get('bpm')}, key {e.get('key')}, {e.get('duration')})")
    e["title"] = "涙のワンサイデッド・ラヴ (Namida no One Sided Love)"
    e["bpm"] = 68
    e["key"] = "E Minor"
    e["duration"] = mmss(291)
    e["coverArt"] = NAMIDA_ART
    e.pop("coverArtFallback", None)
    e["producers"] = "牧村憲一 (Ken'ichi Makimura)"
    e["writers"] = "竹内まりや (Mariya Takeuchi)"
    log(f"    -> {e['title']}  (bpm 68, key E Minor, 4:51)")

    d = fetch_song_json("0009") or {}
    d.update({"id": "9", "title": e["title"], "artist": "Mariya Takeuchi",
              "coverArt": NAMIDA_ART, "duration": 291, "displayDuration": mmss(291),
              "bpm": 68, "key": "E Minor", "year": "1979",
              "producers": e["producers"], "writers": e["writers"],
              "album": "UNIVERSITY STREET"})
    d.pop("coverArtFallback", None)
    if apply:
        (SONGS / "0009.json").write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    written.append("songs/0009.json")

    # ------------------------------------------------- 2. real Plastic Love
    new_id = str(max(int(s["id"]) for s in songs if str(s.get("id")).isdigit()) + 1)
    log(f"[2] adding real Plastic Love as id {new_id} (stems already on the CDN at /Plastic Love/)")
    entry = {
        "id": new_id, "title": "Plastic Love", "artist": "Mariya Takeuchi",
        "coverArt": PLASTIC_ART, "coverArtFallback": PLASTIC_ART_FB,
        "duration": mmss(294), "bpm": 103, "key": "D Minor", "hasStems": True,
        "producers": "山下達郎 (Tatsuro Yamashita)",
        "writers": "竹内まりや (Mariya Takeuchi)",
        "genres": ["city pop", "japanese r&b", "j-pop"],
    }
    songs.append(entry)
    pl = {
        "id": new_id, "title": "Plastic Love", "artist": "Mariya Takeuchi",
        "coverArt": PLASTIC_ART, "coverArtFallback": PLASTIC_ART_FB,
        "duration": 294, "displayDuration": mmss(294), "bpm": 103, "key": "D Minor",
        "year": "1984", "producers": "山下達郎 (Tatsuro Yamashita)",
        "writers": "竹内まりや (Mariya Takeuchi)", "recordLabel": "MOON RECORDS",
        "album": "VARIETY",
        "stems": [{"id": s, "name": s.upper(), "url": f"{CDN}/Plastic%20Love/{s}.m4a"}
                  for s in STEMS],
    }
    if apply:
        (SONGS / f"{int(new_id):04d}.json").write_text(
            json.dumps(pl, indent=1, ensure_ascii=False), encoding="utf-8")
    written.append(f"songs/{int(new_id):04d}.json")

    # ------------------------------------------------------------- 3. id 77
    k = by_id["77"]
    log(f"[3] id 77 cover was DJ Khaled's 'Suffering From Success' art -> STEPS +2 (2014)")
    k["coverArt"] = KOKUBU_ART
    k.pop("coverArtFallback", None)
    kd = fetch_song_json("0077") or {}
    kd.update({"coverArt": KOKUBU_ART, "album": "STEPS +2"})
    kd.pop("coverArtFallback", None)
    if apply:
        (SONGS / "0077.json").write_text(json.dumps(kd, indent=1, ensure_ascii=False), encoding="utf-8")
    written.append("songs/0077.json")

    if not apply:
        log("\n(dry run — pass --apply to write and publish)")
        return

    backup = INDEX.with_name(f"music-index.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    backup.write_bytes(INDEX.read_bytes())
    tmp = INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(cat, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, INDEX)
    log(f"\nbacked up index -> {backup.name}; catalogue now {len(songs)} songs")

    fd, lp = tempfile.mkstemp(suffix=".txt"); os.close(fd)
    # trailing newline: upload-catalog.sh reads with `while read` and drops a
    # final line without one — that silently skipped music-index.json before.
    Path(lp).write_text("\n".join(written + ["music-index.json"]) + "\n", encoding="utf-8")
    r = subprocess.run(["bash", str(REPO / "scripts/upload-catalog.sh"), "--files-from", lp],
                       capture_output=True, text=True, timeout=1800)
    for line in (r.stdout or "").strip().splitlines()[-5:]:
        log("  " + line)

    # prove it landed
    live = json.loads(urllib.request.urlopen(
        f"{CDN}/musicdata/music-index.json?v={int(time.time())}", timeout=60).read().decode("utf-8", "replace"))
    lb = {str(s["id"]): s for s in live["songsList"]}
    log("\nverified live:")
    log(f"  id 9   -> {lb.get('9',{}).get('title')}")
    log(f"  id {new_id} -> {lb.get(new_id,{}).get('title')} by {lb.get(new_id,{}).get('artist')}")
    log(f"  id 77 cover STEPS+2: {lb.get('77',{}).get('coverArt','')==KOKUBU_ART}")
    for s in STEMS:
        u = f"{CDN}/Plastic%20Love/{s}.m4a"
        try:
            req = urllib.request.Request(u, method="HEAD")
            with urllib.request.urlopen(req, timeout=25) as x:
                log(f"  plastic love {s}: {x.status}")
        except Exception as ex:
            log(f"  plastic love {s}: FAIL {ex}")

if __name__ == "__main__":
    main()
