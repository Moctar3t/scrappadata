#!/usr/bin/env python3
"""
audit_fingerprint.py — Audit the Zingy catalogue against AcoustID.

For each published/verified track:
  1. Download the 4 stems from BunnyCDN
  2. Mix them back into a full track (ffmpeg)
  3. Fingerprint the mix (fpcalc / Chromaprint)
  4. Look up the fingerprint on AcoustID → MusicBrainz recording
  5. Compare AcoustID's answer vs our catalogue metadata
  6. Flag mismatches

Run on the VPS:
  python3 audit_fingerprint.py [--limit N] [--offset N]

Requires: fpcalc, ffmpeg, curl (all present on the VPS)
"""
import json, os, subprocess, sys, tempfile, time, urllib.parse, urllib.request
from pathlib import Path

ACOUSTID_KEY = "EDC8Wnn65f"
CDN_HOST = "https://dimesystemsstorage.b-cdn.net"
STEM_PREFIX = "catalogtracks"
STEMS = ["bass", "drums", "other", "vocals"]
STATE_PATH = Path(__file__).resolve().parent / "ingest_gpu_state.json"

# MusicBrainz asks for a meaningful User-Agent
MB_UA = "ZingyAudit/1.0 (dime@zingy.app)"


def log(msg):
    print(msg, flush=True)


def stem_url(uuid: str, stem: str) -> str:
    return f"{CDN_HOST}/{STEM_PREFIX}/{uuid}/{stem}.m4a"


def download_file(url: str, dest: str) -> bool:
    """Download a file from URL to local path."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": MB_UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            with open(dest, "wb") as f:
                f.write(resp.read())
        return os.path.getsize(dest) > 1000
    except Exception as e:
        return False


def mix_stems(stem_paths: list[str], output: str) -> bool:
    """Mix 4 stems back into a single audio file using ffmpeg."""
    inputs = []
    for p in stem_paths:
        inputs.extend(["-i", p])
    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", f"amix=inputs={len(stem_paths)}:duration=longest:normalize=0",
        "-ac", "2", "-ar", "44100",
        output
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=60)
    return r.returncode == 0 and os.path.exists(output)


def fingerprint(audio_path: str) -> tuple[str | None, float | None]:
    """Run fpcalc and return (fingerprint, duration)."""
    try:
        r = subprocess.run(
            ["fpcalc", "-json", audio_path],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode != 0:
            return None, None
        data = json.loads(r.stdout)
        return data.get("fingerprint"), data.get("duration")
    except Exception:
        return None, None


def acoustid_lookup(fp: str, duration: float) -> list[dict]:
    """Query AcoustID and return list of matched recordings."""
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
    """Lowercase, strip punctuation for fuzzy comparison."""
    import re, unicodedata
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def compare(track: dict, results: list[dict]) -> dict:
    """Compare AcoustID results against our catalogue metadata."""
    our_title = normalize(track.get("title", ""))
    our_artist = normalize(track.get("artist", ""))

    if not results:
        return {"match": "no_result", "detail": "AcoustID returned nothing"}

    best = results[0]
    score = best.get("score", 0)
    recordings = best.get("recordings", [])

    if not recordings:
        return {"match": "no_recordings", "score": score,
                "detail": "AcoustID matched audio but no MusicBrainz recording linked"}

    # Check if ANY recording matches our metadata
    for rec in recordings:
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
                "score": score,
                "acoustid_title": rec.get("title"),
                "acoustid_artist": ", ".join(a.get("name", "") for a in rec.get("artists", [])),
                "mb_id": rec.get("id"),
            }

    # No recording matched — this is a potential mismatch
    top = recordings[0]
    return {
        "match": "MISMATCH",
        "score": score,
        "our_title": track.get("title"),
        "our_artist": track.get("artist"),
        "acoustid_title": top.get("title"),
        "acoustid_artist": ", ".join(a.get("name", "") for a in top.get("artists", [])),
        "mb_id": top.get("id"),
        "all_recordings": len(recordings),
    }


def audit_track(track: dict, idx: int, total: int) -> dict:
    """Full audit of a single track."""
    uuid = track.get("uuid")
    cid = track.get("catalog_id")
    label = f"{track.get('artist', '?')} — {track.get('title', '?')}"

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Download stems
        stem_paths = []
        for stem in STEMS:
            url = stem_url(uuid, stem)
            dest = os.path.join(tmpdir, f"{stem}.m4a")
            if not download_file(url, dest):
                return {"id": cid, "track": label, "match": "download_failed",
                        "detail": f"could not download {stem}"}
            stem_paths.append(dest)

        # 2. Mix stems
        mix_path = os.path.join(tmpdir, "mix.wav")
        if not mix_stems(stem_paths, mix_path):
            return {"id": cid, "track": label, "match": "mix_failed"}

        # 3. Fingerprint
        fp, dur = fingerprint(mix_path)
        if not fp:
            return {"id": cid, "track": label, "match": "fingerprint_failed"}

        # 4. AcoustID lookup
        time.sleep(0.35)  # respect 3 req/s limit
        results = acoustid_lookup(fp, dur)

        # 5. Compare
        result = compare(track, results)
        result["id"] = cid
        result["track"] = label

    return result


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Audit Zingy catalogue against AcoustID")
    ap.add_argument("--limit", type=int, default=50, help="Max tracks to audit")
    ap.add_argument("--offset", type=int, default=0, help="Skip N tracks")
    ap.add_argument("--status", default="verified,published",
                    help="Comma-separated statuses to audit")
    args = ap.parse_args()

    # Load state
    if not STATE_PATH.exists():
        sys.exit("No state file found. Run from the scrappadata directory.")
    state = json.load(open(STATE_PATH, encoding="utf-8"))

    statuses = set(args.status.split(","))
    tracks = [t for t in state["tracks"] if t.get("status") in statuses and t.get("uuid")]
    tracks = tracks[args.offset:args.offset + args.limit]

    if not tracks:
        log("No tracks to audit."); return

    log(f"═══ CHROMAPRINT AUDIT: {len(tracks)} tracks ═══")
    log(f"    (downloading stems from CDN → mix → fingerprint → AcoustID lookup)\n")

    confirmed = mismatches = no_result = errors = 0
    mismatch_list = []

    for i, t in enumerate(tracks, 1):
        result = audit_track(t, i, len(tracks))
        m = result.get("match", "error")

        if m == "confirmed":
            confirmed += 1
            log(f"  ✓ [{i}/{len(tracks)}] {result['track']}  "
                f"(score={result.get('score', 0):.2f})")
        elif m == "MISMATCH":
            mismatches += 1
            mismatch_list.append(result)
            log(f"  ✗ [{i}/{len(tracks)}] {result['track']}")
            log(f"      AcoustID says: {result.get('acoustid_artist')} — {result.get('acoustid_title')}")
            log(f"      score={result.get('score', 0):.2f}, mb_id={result.get('mb_id')}")
        elif m in ("no_result", "no_recordings"):
            no_result += 1
            log(f"  · [{i}/{len(tracks)}] {result['track']}  ({result.get('detail', m)})")
        else:
            errors += 1
            log(f"  ! [{i}/{len(tracks)}] {result['track']}  ({m}: {result.get('detail', '')})")

    log(f"\n═══ RESULTS ═══")
    log(f"  ✓ Confirmed:  {confirmed}")
    log(f"  ✗ Mismatch:   {mismatches}")
    log(f"  · Not found:  {no_result}")
    log(f"  ! Errors:     {errors}")

    if mismatch_list:
        log(f"\n═══ MISMATCHES ({len(mismatch_list)}) ═══")
        for r in mismatch_list:
            log(f"  ID {r['id']}: {r['track']}")
            log(f"    → AcoustID: {r.get('acoustid_artist')} — {r.get('acoustid_title')}")


if __name__ == "__main__":
    main()
