#!/usr/bin/env python3
"""
audit_full_length.py — Audit the Zingy catalogue against AcoustID using ORIGINAL audio.

For each published/verified track:
  1. Download the original full-length audio from YouTube using `track['source']['url']`.
  2. Fingerprint the pristine original audio (fpcalc).
  3. Look up the fingerprint on AcoustID → MusicBrainz recording.
  4. Compare AcoustID's answer vs our catalogue metadata.
  5. Flag mismatches.

Run on the VPS:
  python3 audit_full_length.py [--limit N]
"""
import json, os, subprocess, sys, tempfile, time, urllib.parse, urllib.request
from pathlib import Path

ACOUSTID_KEY = "EDC8Wnn65f"
STATE_PATH = Path("/tmp/ingest_gpu_state.json")
MB_UA = "ZingyAudit/1.0 (dime@zingy.app)"

def log(msg):
    print(msg, flush=True)

def download_original(url: str, dest: str) -> bool:
    """Download original audio from YouTube."""
    cmd = ["yt-dlp", "-x", "--audio-format", "mp3", url, "-o", dest]
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0 and os.path.exists(dest)

def fingerprint(audio_path: str) -> tuple[str | None, float | None]:
    try:
        r = subprocess.run(["fpcalc", "-json", audio_path], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None, None
        data = json.loads(r.stdout)
        return data.get("fingerprint"), data.get("duration")
    except Exception:
        return None, None

def acoustid_lookup(fp: str, duration: float) -> list[dict]:
    params = urllib.parse.urlencode({
        "client": ACOUSTID_KEY,
        "fingerprint": fp,
        "duration": int(duration),
        "meta": "recordings+releasegroups+compress"
    })
    url = f"https://api.acoustid.org/v2/lookup?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": MB_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("results", [])
    except Exception as e:
        log(f"    ! AcoustID error: {e}")
        return []

def normalize(s: str) -> str:
    import re, unicodedata
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()

def compare(track: dict, results: list[dict]) -> dict:
    our_title = normalize(track.get("title", ""))
    our_artist = normalize(track.get("artist", ""))

    if not results:
        return {"match": "no_result", "detail": "AcoustID returned NO matches for this audio."}

    # Gather all MB recordings from all high-scoring results
    all_recordings = []
    for res in results:
        if res.get("score", 0) > 0.5:
            all_recordings.extend(res.get("recordings", []))

    if not all_recordings:
        return {"match": "no_recordings", "score": results[0].get("score", 0),
                "detail": "Matched audio blob on AcoustID, but it's not linked to an official MusicBrainz ID."}

    # Check if ANY recording matches our metadata
    for rec in all_recordings:
        rec_title = normalize(rec.get("title", ""))
        rec_artists = [normalize(a.get("name", "")) for a in rec.get("artists", [])]

        title_match = (our_title in rec_title or rec_title in our_title
                       or set(our_title.split()) & set(rec_title.split()))
        artist_match = any(
            a in our_artist or our_artist in a
            or set(a.split()) & set(our_artist.split())
            for a in rec_artists
        ) if rec_artists else False

        if title_match and artist_match:
            return {
                "match": "confirmed",
                "score": results[0].get("score", 0),
                "acoustid_title": rec.get("title"),
                "acoustid_artist": ", ".join(a.get("name", "") for a in rec.get("artists", [])),
                "mb_id": rec.get("id"),
            }

    # No recording matched — MISMATCH
    top_rec = all_recordings[0]
    return {
        "match": "MISMATCH",
        "score": results[0].get("score", 0),
        "acoustid_title": top_rec.get("title"),
        "acoustid_artist": ", ".join(a.get("name", "") for a in top_rec.get("artists", [])),
        "mb_id": top_rec.get("id"),
    }

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--offset", type=int, default=0)
    args = ap.parse_args()

    if not STATE_PATH.exists():
        sys.exit(f"State file {STATE_PATH} missing.")
    state = json.load(open(STATE_PATH, encoding="utf-8"))

    # Only test tracks that have a known source URL
    tracks = [t for t in state["tracks"] 
              if t.get("status") in ("verified", "published") 
              and t.get("source") and t["source"].get("url")]
    
    tracks = tracks[args.offset:args.offset + args.limit]
    if not tracks:
        log("No tracks to audit."); return

    log(f"═══ FULL-LENGTH AUDIT: {len(tracks)} tracks ═══")
    log("    (Downloading original YT audio → fpcalc → AcoustID)\n")

    confirmed = mismatches = no_result = errors = 0
    mismatch_list = []

    for i, t in enumerate(tracks, 1):
        cid = t.get("catalog_id")
        label = f"{t.get('artist', '?')} — {t.get('title', '?')}"
        url = t["source"]["url"]

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = os.path.join(tmpdir, "original.mp3")
            if not download_original(url, dest):
                log(f"  ! [{i}/{len(tracks)}] {label}  (Failed to download from YT)")
                errors += 1
                continue
            
            fp, dur = fingerprint(dest)
            if not fp:
                log(f"  ! [{i}/{len(tracks)}] {label}  (Fingerprint failed)")
                errors += 1
                continue

            time.sleep(0.35)
            results = acoustid_lookup(fp, dur)
            res = compare(t, results)

            m = res.get("match")
            if m == "confirmed":
                confirmed += 1
                log(f"  ✓ [{i}/{len(tracks)}] {label}  (score={res.get('score', 0):.2f})")
            elif m == "MISMATCH":
                mismatches += 1
                mismatch_list.append({"track": label, "acoustid": f"{res.get('acoustid_artist')} - {res.get('acoustid_title')}"})
                log(f"  ✗ [{i}/{len(tracks)}] {label}")
                log(f"      AcoustID says: {res.get('acoustid_artist')} — {res.get('acoustid_title')}")
            elif m in ("no_result", "no_recordings"):
                no_result += 1
                log(f"  · [{i}/{len(tracks)}] {label}  ({res.get('detail')})")
            else:
                errors += 1
                log(f"  ! [{i}/{len(tracks)}] {label}  ({m})")

    log(f"\n═══ RESULTS ═══")
    log(f"  ✓ Confirmed:  {confirmed}")
    log(f"  ✗ Mismatch:   {mismatches}")
    log(f"  · No DB link: {no_result}")
    log(f"  ! Errors:     {errors}")

if __name__ == "__main__":
    main()
