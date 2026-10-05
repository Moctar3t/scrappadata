#!/usr/bin/env python3
"""Catalog-wide FAKE / WRONG-SONG / COVER detector (+ optional auto-fix).

Why this exists: the gate protects NEW ingests, but the catalogue still contains
songs from the pre-gate era (blind first-YouTube-hit downloads, stems linked from
legacy CDN folders with `split_source='provided'` and no verification). Case study:
"Travis Scott — I KNOW ?" shipped a bootleg because its legacy stems were linked
without ever passing a check.

Detection: for every app song, compare its duration against the OFFICIAL recording
length (Deezer/iTunes via meta_sources.authoritative_duration, validated match).
The duration in the app came from the ingested audio, so
    |app duration − official duration| > tolerance  ⇒  it's not the recording.
Legacy-folder stems (not under catalogtracks/…) are also reported as 'unverified'.

Usage:
    python3 audit_fakes.py                  # scan + report + fake_suspects.json
    python3 audit_fakes.py --tolerance 6    # stricter/looser delta (default 7s)
    python3 audit_fakes.py --fix            # re-pick a VERIFIED source through the
                                            # gate for each suspect and requeue it on
                                            # the GPU queue (bpm/key recomputed);
                                            # finish with export + publish as usual
    python3 audit_fakes.py --fix --only 905 # fix one song by app id
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys, urllib.parse, urllib.request
from pathlib import Path

from meta_sources import authoritative_durations, save_cache

HERE = Path(__file__).resolve().parent
REPO = Path("/Users/dime/Developer/ios-zingy")
IDX = REPO / "App/ZingyApp/Resources/musicdata/music-index.json"
SONGS = REPO / "App/ZingyApp/Resources/musicdata/songs"
VPS = "204.168.236.140"
API = "https://zingy.dime.systems/api"


def psql(q: str) -> str:
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=12", f"root@{VPS}",
                        f"sudo -u postgres psql -d zingy -tA -c \"{q}\""],
                       capture_output=True, text=True)
    return r.stdout.strip()


def dur_s(v) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    try:
        mm, ss = str(v or "0:00").split(":")
        return int(mm) * 60 + int(ss)
    except Exception:
        return 0


def stem_info(app_id: str) -> tuple[str, str | None]:
    """→ (origin, first stem url)."""
    p = SONGS / f"{int(app_id):04d}.json" if str(app_id).isdigit() else None
    try:
        d = json.load(open(p, encoding="utf-8"))
        u = (d.get("stems") or [{}])[0].get("url", "")
        return ("catalogtracks" if "/catalogtracks/" in u else ("legacy" if u else "none")), u or None
    except Exception:
        return "?", None


def actual_audio_s(url: str) -> int | None:
    """Estimate the REAL length of the audio on the CDN from Content-Length.
    Stems are CBR-ish: mp3 = 192kbps (24000 B/s), m4a = 128kbps (16000 B/s).
    Coarse (±10%), but a wrong recording is rarely within 10% — and this catches
    fakes whose METADATA duration was copied from Spotify and therefore 'matches'."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as r:
            size = int(r.headers.get("Content-Length") or 0)
        if not size:
            return None
        rate = 16000 if url.lower().endswith(".m4a") else 24000
        return round(size / rate)
    except Exception:
        return None


def scan(tolerance: int, only: str | None) -> list[dict]:
    songs = json.load(open(IDX, encoding="utf-8"))["songsList"]
    if only:
        songs = [s for s in songs if str(s.get("id")) == str(only)]
    suspects = []
    print(f"[audit] scanning {len(songs)} songs (tolerance ±{tolerance}s)…")
    for i, s in enumerate(songs, 1):
        if not s.get("hasStems"):
            continue
        claimed = dur_s(s.get("duration"))
        if not claimed:
            continue
        try:
            officials = authoritative_durations(s.get("artist", ""), s.get("title", ""))
        except Exception:
            officials = []
        if i % 100 == 0:
            print(f"  …{i}/{len(songs)}")
            save_cache()
        if not officials:
            continue                      # unknown song — can't judge by duration
        origin, stem_url = stem_info(s["id"])
        reasons = []
        # compare against the CLOSEST official version (radio edit vs extended etc.)
        off_m = min(officials, key=lambda o: abs(claimed - o))
        delta = abs(claimed - off_m)
        if delta > tolerance:
            reasons.append(f"metadata {claimed}s vs official {off_m}s (Δ{delta}s)")
        # second net: the AUDIO itself — catches fakes with copied-in metadata
        actual = actual_audio_s(stem_url) if stem_url else None
        if actual:
            off_a = min(officials, key=lambda o: abs(actual - o))
            if abs(actual - off_a) > max(tolerance + 8, round(off_a * 0.10)):
                reasons.append(f"audio ≈{actual}s vs official {off_a}s")
        if reasons:
            suspects.append({
                "app_id": str(s["id"]), "artist": s.get("artist", ""),
                "title": s.get("title", ""), "claimed_s": claimed,
                "official_s": off_m, "officials": officials, "audio_s": actual,
                "delta_s": delta, "stems": origin, "reasons": reasons,
            })
            print(f"  [SUSPECT] #{s['id']} {s.get('artist','?')} — {s.get('title','?')}"
                  f"  ({origin})  {' | '.join(reasons)}")
    save_cache()
    return suspects


def requeue_fix(sp: dict, id_map: dict, secret: str) -> bool:
    """Gate-pick a verified source for a suspect and requeue it (in place when the
    song is DB-backed, else a fresh enqueue pinned to the same app id)."""
    from ingest_playlist import Track, search_candidates, pick as decide
    from ytmusicapi import YTMusic
    t = Track(spotify_id="", title=sp["title"],
              artists=[a.strip() for a in re.split(r"[;,]", sp["artist"]) if a.strip()],
              album="", duration_ms=sp["official_s"] * 1000)
    decide(t, search_candidates(t, 8, YTMusic()))
    if t.status != "accepted" or not t.youtube_url:
        print(f"    ✗ gate found no trustworthy source ({t.status}: {'; '.join(t.reasons[:2])})"
              f" — fix manually with a known-good link")
        return False
    print(f"    ✓ gate picked [{t.source_kind}] {t.youtube_url}")
    uuid = {v: k for k, v in id_map.items()}.get(sp["app_id"])
    if uuid:
        psql(f"UPDATE tracks SET original_url='{t.youtube_url}', status='queued', "
             f"split_source=NULL, bpm=NULL, song_key=NULL, attempts=0, error=NULL, "
             f"duration_sec={sp['official_s']} WHERE id='{uuid}'")
        print(f"    → requeued DB row {uuid[:8]}… (app id stays {sp['app_id']})")
        return True
    # legacy-only song: fresh enqueue, then pin the new uuid to the SAME app id so
    # export updates the existing entry instead of duplicating it.
    songf = SONGS / f"{int(sp['app_id']):04d}.json"
    detail = json.load(open(songf, encoding="utf-8")) if songf.exists() else {}
    payload = {"youtubeUrl": t.youtube_url, "title": sp["title"], "artist": sp["artist"],
               "durationSec": sp["official_s"], "coverUrl": detail.get("coverArt")}
    req = urllib.request.Request(API + "/internal/split/catalog",
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "x-worker-secret": secret})
    with urllib.request.urlopen(req, timeout=20) as r:
        new_uuid = json.load(r).get("id")
    id_map[new_uuid] = sp["app_id"]
    json.dump(id_map, open(HERE / "catalog_id_map.json", "w", encoding="utf-8"),
              indent=0, ensure_ascii=False)
    print(f"    → enqueued {new_uuid[:8]}… pinned to app id {sp['app_id']}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tolerance", type=int, default=7, help="max |app−official| seconds")
    ap.add_argument("--fix", action="store_true", help="gate-repick + requeue suspects")
    ap.add_argument("--only", help="restrict to one app id")
    a = ap.parse_args()

    suspects = scan(a.tolerance, a.only)
    out = HERE / "fake_suspects.json"
    json.dump(suspects, open(out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(f"\n[audit] {len(suspects)} suspect(s) → {out}")
    if not suspects or not a.fix:
        if suspects:
            print("[audit] run again with --fix to gate-repick + requeue them")
        return

    secret = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=12", f"root@{VPS}",
         "grep '^WORKER_SECRET=' /var/www/zingy-api/.env | cut -d= -f2"],
        capture_output=True, text=True).stdout.strip()
    if not secret:
        sys.exit("couldn't read WORKER_SECRET from the VPS")
    id_map = json.load(open(HERE / "catalog_id_map.json", encoding="utf-8"))
    fixed = 0
    for sp in suspects:
        print(f"[fix] #{sp['app_id']} {sp['artist']} — {sp['title']}")
        try:
            fixed += 1 if requeue_fix(sp, id_map, secret) else 0
        except Exception as e:
            print(f"    ✗ error: {e}")
    print(f"\n[audit] {fixed}/{len(suspects)} requeued. The GPU worker will re-split them;"
          f"\n        then: python3 export_catalog.py && bash {REPO}/scripts/upload-catalog.sh")


if __name__ == "__main__":
    main()
