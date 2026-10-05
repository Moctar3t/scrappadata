#!/usr/bin/env python3
"""
build_add_csv.py — turn "add this album / these songs" into the same Exportify-
shaped CSV the ingest pipeline already trusts.

The gate refuses any source it cannot verify against a known runtime, so a
request like "add the xperiment album" has to acquire real durations first.
Spotify is the authority we already use for that, so we ask it directly instead
of letting the pipeline guess.

Audio features (danceability/energy/…) are NOT fetched: Spotify closed that
endpoint to new apps in Nov 2024. Those columns are left EMPTY rather than
filled with anything invented — the ingest writes absent fields as absent.
"""
from __future__ import annotations
import base64, csv, json, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

COLUMNS = ["Track URI", "Track Name", "Album Name", "Artist Name(s)", "Release Date",
           "Duration (ms)", "Popularity", "Explicit", "Added By", "Added At", "Genres",
           "Record Label", "Danceability", "Energy", "Key", "Loudness", "Mode",
           "Speechiness", "Acousticness", "Instrumentalness", "Liveness", "Valence",
           "Tempo", "Time Signature"]

def token() -> str:
    """The playlist owner's token when someone has logged in (spotify_intake), else
    the app's own. The keys live in .spotify.json, never in this file."""
    import spotify_intake
    return spotify_intake.bearer()

def api(tok: str, path: str, **params):
    url = f"https://api.spotify.com/v1/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    for _ in range(3):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=25).read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3); continue
            raise
        except Exception:
            time.sleep(2)
    return {}

def row(tr: dict, album: dict | None = None) -> dict:
    alb = album or tr.get("album") or {}
    return {
        "Track URI": tr.get("uri", ""),
        "Track Name": tr.get("name", ""),
        "Album Name": alb.get("name", ""),
        # ";" separates collaborators, matching the catalogue's convention
        "Artist Name(s)": ";".join(a["name"] for a in tr.get("artists", [])),
        "Release Date": (alb.get("release_date") or ""),
        "Duration (ms)": tr.get("duration_ms", ""),
        "Popularity": tr.get("popularity", ""),
        "Explicit": str(tr.get("explicit", False)).lower(),
        "Added By": "", "Added At": "", "Genres": "", "Record Label": alb.get("label", ""),
        "Danceability": "", "Energy": "", "Key": "", "Loudness": "", "Mode": "",
        "Speechiness": "", "Acousticness": "", "Instrumentalness": "", "Liveness": "",
        "Valence": "", "Tempo": "", "Time Signature": "",
    }

def find_album(tok, artist, name):
    """Resolve an album by name, EXACTLY.

    Two traps this exists for:
      · A raw Spotify album id/URL should be honoured verbatim.
      · "xperiment - cartunez" and "xperiment" are two different releases by the
        same artist (35 tracks vs 22). Searching the shortened name and taking
        the first hit silently ingests the wrong, smaller edition — so an exact
        name match wins, and a partial match is never assumed to be right.
    """
    # a pasted id or URL
    raw = name.strip()
    m = re.search(r"(?:album[:/])([A-Za-z0-9]{22})", raw) or re.fullmatch(r"([A-Za-z0-9]{22})", raw)
    if m:
        alb = api(tok, f"albums/{m.group(1)}")
        if alb.get("id"):
            return alb

    cands = []
    try:
        res = api(tok, "search", q=f"album:{name} artist:{artist}", type="album", limit=10)
        cands += (res.get("albums") or {}).get("items", [])
    except Exception:
        pass
    # The album-search endpoint is flaky for some queries; a track search reaches
    # the same albums and is what actually surfaced the 35-track edition.
    try:
        res = api(tok, "search", q=f"{artist} {name}", type="track", limit=10)
        for t in (res.get("tracks") or {}).get("items", []):
            if t.get("album"):
                cands.append(t["album"])
    except Exception:
        pass

    want = re.sub(r"[^a-z0-9]", "", name.lower())
    seen, uniq = set(), []
    for a in cands:
        if a.get("id") and a["id"] not in seen:
            seen.add(a["id"]); uniq.append(a)
    exact = [a for a in uniq if re.sub(r"[^a-z0-9]", "", a["name"].lower()) == want]
    if exact:                       # prefer the fullest pressing of the right album
        exact.sort(key=lambda a: -(a.get("total_tracks") or 0))
        return exact[0]
    if uniq:
        near = sorted(uniq, key=lambda a: -(a.get("total_tracks") or 0))
        print(f"  ! no album is named exactly {name!r}. Closest matches:")
        for a in near[:6]:
            print(f"      {a['name']!r}  ({a.get('total_tracks')} tracks)")
        print("    refusing to guess — pass the exact name or the Spotify album id.")
    return None

def find_track(tok, artist, title, want_ms=None):
    res = api(tok, "search", q=f"track:{title} artist:{artist}", type="track", limit=10)
    items = (res.get("tracks") or {}).get("items", [])
    if not items:
        res = api(tok, "search", q=f"{artist} {title}", type="track", limit=10)
        items = (res.get("tracks") or {}).get("items", [])
    if not items:
        return None
    if want_ms:
        items.sort(key=lambda t: abs(t.get("duration_ms", 0) - want_ms))
    return items[0]

def build(spec: dict, out_name: str = "add_batch.csv") -> Path:
    """spec = {"albums": [(artist, album), ...], "tracks": [(artist, title, want_ms), ...]}

    Returns the path to an Exportify-shaped CSV. Durations come from Spotify so
    the ingest gate has a real runtime to verify each source against — without
    one the gate cannot accept anything, by design.
    """
    tok = token()
    rows = []
    for artist, name in spec.get("albums", []):
        alb = find_album(tok, artist, name)
        if not alb:
            print(f"  ! album not found: {artist} / {name}")
            continue
        full = api(tok, f"albums/{alb['id']}")
        label = full.get("label", "")
        print(f"  album: {full.get('name')} — {full.get('total_tracks')} tracks "
              f"({full.get('release_date')})")
        for t in (full.get("tracks") or {}).get("items", []):
            det = api(tok, f"tracks/{t['id']}")
            r = row(det or t, {**full, "label": label})
            rows.append(r)
            print(f"     {r['Track Name'][:34]:34s} {int(r['Duration (ms)'] or 0)/1000:6.1f}s")
    for artist, title, want in spec.get("tracks", []):
        t = find_track(tok, artist, title, want)
        if not t:
            print(f"  ! track not found: {artist} — {title}")
            continue
        r = row(t)
        rows.append(r)
        print(f"     {r['Artist Name(s)'][:24]:24s} {r['Track Name'][:28]:28s} "
              f"{t.get('duration_ms',0)/1000:6.1f}s")
    seen, uniq = set(), []
    for r in rows:
        k = r["Track URI"] or (r["Track Name"], r["Artist Name(s)"])
        if k in seen:
            continue
        seen.add(k); uniq.append(r)
    out = HERE / out_name
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in uniq:
            w.writerow(r)
    print(f"  wrote {out.name}: {len(uniq)} unique track(s)")
    return out

def main():
    build({"albums": [("Ken Carson", "xperiment")],
           "tracks": [("The Smiths", "How Soon Is Now?", 408000),
                      ("rommulas", "PALETA", 88000),
                      ("Kanye West", "FATHER", 169000),
                      ("Fred again..", "Victory Lap", 166000),
                      ("Shinsei Kamattechan", "Yugure no Tori", 304000)]})

if __name__ == "__main__":
    main()
