#!/usr/bin/env python3
"""
reconcile_metadata.py — multi-source metadata reconciliation ("clash") for the Zingy
catalog.

Instead of trusting ONE source per field (the old pipeline: iTunes for art, Genius for
producers, librosa for BPM…), this queries EVERY available source for EVERY field and
cross-checks them. Agreement → confident. Disagreement → resolve by source authority
and flag for review. Membership wins for album/single (a track that appears on a real
multi-track album is an Album track even if it was also released as a single — this is
what fixes the ~89%-defaulted-to-"Single" bug).

Sources (all keyless):
  • Genius      (public web API + browser UA) — producers, album, year
  • iTunes      (Search API)                  — album/collectionType, year, genre
  • Deezer      (public API)                  — album record_type, BPM, year
  • MusicBrainz (ws/2, 1 req/s)               — release-group primary-type, year, label
  • Spotify     (audio-features) — ONLY if SPOTIFY_CLIENT_ID/SECRET in env (BPM + key)

Fields reconciled: release format, bpm, key, producers (multi-value), year, label.

Modes:
  --dry-run (default)  writes reconcile_report.{json,md}; NO catalog writes.
  --apply              backs up musicdata/ to musicdata.bak-<ts>/ then writes in place.
  --limit N            only the first N catalog tracks (sampling)
  --ids 2,3,10         only these catalog ids
  --match "preacher"   only tracks whose title contains this (case-insensitive)
  --workers N          fast-source thread pool size (default 6; MusicBrainz stays 1/s)

Cache: reconcile_cache.json (extracted per-source results; resumable + idempotent).
After --apply, re-publish with ios-zingy/scripts/upload-catalog.sh.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Lock

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
INDEX = os.path.join(APP, "music-index.json")
RELEASE = os.path.join(APP, "release-formats.json")
SONGS = os.path.join(APP, "songs")
CACHE_PATH = os.path.join(ROOT, "reconcile_cache.json")
REPORT_JSON = os.path.join(ROOT, "reconcile_report.json")
REPORT_MD = os.path.join(ROOT, "reconcile_report.md")

BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
MB_UA = {"User-Agent": "ZingyMetaReconcile/1.0 ( https://dime.systems )"}

PRODUCER_JUNK = {"self-produced", "self produced", "selfproduced", "n/a", "na", "none",
                 "null", "search pending", "unknown", "tba", "", "?", "various"}
PROD_ROLES = {"producer", "co-producer", "additional production", "additional producer",
              "executive producer", "vocal producer"}

# ---------------------------------------------------------------- HTTP + rate limiting
_rl_lock = Lock()
_last_call = {}


def _rate(host, interval):
    with _rl_lock:
        wait = interval - (time.time() - _last_call.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.time()


def http_json(url, headers=BROWSER_UA, host="default", interval=0.2, timeout=12, retries=2):
    for attempt in range(retries + 1):
        _rate(host, interval)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            # Broad on purpose: a network scraper must never let one slow/odd response
            # (incl. py3.9 socket.timeout, which is NOT a TimeoutError) drop a whole track.
            if attempt == retries:
                return None
            time.sleep(0.8 * (attempt + 1))
    return None


# ---------------------------------------------------------------- normalization / match
def _norm(s):
    s = (s or "").lower()
    s = re.sub(r"\(feat[^)]*\)", " ", s)
    s = re.sub(r"\bfeat\.?\b.*$", " ", s)
    s = re.sub(r"\(.*?(remaster|remix|version|edit|deluxe|live).*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _title_matches(query_title, result_title):
    a, b = set(_norm(query_title).split()), set(_norm(result_title).split())
    if not a or not b:
        return False
    return len(a & b) / len(a) >= 0.6  # most query-title words appear in the result


def _clean_album(name):
    name = re.sub(r"\s*-\s*(Single|EP)\s*$", "", name or "", flags=re.I)
    name = re.sub(r"\s*\((Deluxe|Expanded|Remastered|Special|Anniversary)[^)]*\)\s*$", "", name, flags=re.I)
    return name.strip()


def _year_from(s):
    m = re.search(r"(19|20)\d{2}", str(s or ""))
    return int(m.group(0)) if m else None


def _artist_match(album_artist, track_artist):
    """True if the album's credited artist is (one of) the track's artist(s). Missing
    album-artist → True (benefit of the doubt). 'Various Artists' or a genuinely
    different artist → False: the track only APPEARS on that release (a compilation or
    playlist), it's not the track's own album."""
    if not album_artist:
        return True
    aa = _norm(album_artist)
    if not aa or aa in ("various artists", "various", "va", "multi interpretes",
                        "verschiedene interpreten", "varios artistas"):
        return False
    parts = [p.strip() for p in re.split(r"[;,&]| feat | ft | x ", _norm(track_artist)) if p.strip()]
    for p in parts:
        if p and (p in aa or aa in p):
            return True
    return False


# ---------------------------------------------------------------- source queries
def src_itunes(artist, title):
    q = urllib.parse.quote(f"{artist} {title}")
    d = http_json(f"https://itunes.apple.com/search?term={q}&entity=song&limit=5",
                  host="itunes", interval=0.25)
    if not d:
        return None
    for r in d.get("results", []):
        if _title_matches(title, r.get("trackName", "")):
            coll = r.get("collectionName", "") or ""
            is_single = coll.lower().endswith("- single") or coll.lower().endswith("- ep")
            return {
                "album": _clean_album(coll) if coll else None,
                "album_artist": r.get("collectionArtistName") or r.get("artistName"),
                "type": "single" if is_single else ("album" if coll else None),
                "ntracks": r.get("trackCount"),
                "year": _year_from(r.get("releaseDate")),
                "genre": r.get("primaryGenreName"),
            }
    return {}


def src_deezer(artist, title):
    q = urllib.parse.quote(f'artist:"{artist}" track:"{title}"')
    d = http_json(f"https://api.deezer.com/search?q={q}&limit=5", host="deezer", interval=0.2)
    if not d or not d.get("data"):
        d = http_json(f"https://api.deezer.com/search?q={urllib.parse.quote(artist + ' ' + title)}&limit=5",
                      host="deezer", interval=0.2)
    if not d:
        return None
    for r in d.get("data", []):
        if _title_matches(title, r.get("title", "")):
            out = {"bpm": r.get("bpm") or 0, "album": None, "album_artist": None,
                   "type": None, "ntracks": None, "year": None}
            alb = r.get("album", {})
            if alb.get("id"):
                ad = http_json(f"https://api.deezer.com/album/{alb['id']}", host="deezer", interval=0.2)
                if ad:
                    out["album"] = _clean_album(ad.get("title"))
                    out["album_artist"] = (ad.get("artist") or {}).get("name")
                    out["type"] = (ad.get("record_type") or "").lower() or None  # single/album/ep
                    out["ntracks"] = ad.get("nb_tracks")
                    out["year"] = _year_from(ad.get("release_date"))
            return out
    return {}


def src_genius(artist, title):
    q = urllib.parse.quote(f"{artist} {title}")
    d = http_json(f"https://genius.com/api/search/multi?q={q}", host="genius", interval=0.3)
    if not d:
        return None
    sid = None
    for s in d.get("response", {}).get("sections", []):
        if s.get("type") == "song" and s.get("hits"):
            hit = s["hits"][0]["result"]
            if _title_matches(title, hit.get("title", "")):
                sid = hit["id"]
            break
    if not sid:
        return {}
    d = http_json(f"https://genius.com/api/songs/{sid}", host="genius", interval=0.3)
    if not d:
        return None
    song = d.get("response", {}).get("song", {})
    names = [p["name"] for p in song.get("producer_artists", [])]
    for perf in song.get("custom_performances", []):
        if perf.get("label", "").strip().lower() in PROD_ROLES:
            names += [a["name"] for a in perf.get("artists", [])]
    alb = (song.get("album") or {}).get("name")
    return {
        "producers": _dedup_names(names),
        "album": _clean_album(alb) if alb else None,
        "year": _year_from(song.get("release_date") or
                            (song.get("release_date_components") or {}).get("year")),
    }


def src_musicbrainz(artist, title):
    q = urllib.parse.quote(f'artist:"{artist}" AND recording:"{title}"')
    d = http_json(f"https://musicbrainz.org/ws/2/recording?query={q}&fmt=json&limit=5",
                  headers=MB_UA, host="musicbrainz", interval=1.1)
    if not d:
        return None
    for rec in d.get("recordings", []):
        if not _title_matches(title, rec.get("title", "")):
            continue
        best = None
        for rel in rec.get("releases", []):
            rg = rel.get("release-group", {}) or {}
            ptype = (rg.get("primary-type") or "").lower()  # single / album / ep / broadcast
            sec = [t.lower() for t in (rg.get("secondary-types") or [])]
            cand = {
                "album": _clean_album(rg.get("title") or rel.get("title")),
                "type": ptype or None,
                "secondary": sec,
                "year": _year_from(rel.get("date")),
            }
            # Prefer an album/ep release-group over a standalone single for naming.
            if best is None or (cand["type"] in ("album", "ep") and best["type"] not in ("album", "ep")):
                best = cand
        return best or {}
    return {}


# ---------------------------------------------------------------- Spotify (optional)
_spotify_token = {"value": None, "exp": 0}


def _spotify_creds():
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    sec = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if cid and sec:
        return cid, sec
    envf = "/Users/dime/Developer/ios-zingy/scripts/.env"
    if os.path.exists(envf):
        kv = {}
        for line in open(envf):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                kv[k] = v.strip().strip('"').strip("'")
        if kv.get("SPOTIFY_CLIENT_ID") and kv.get("SPOTIFY_CLIENT_SECRET"):
            return kv["SPOTIFY_CLIENT_ID"], kv["SPOTIFY_CLIENT_SECRET"]
    return None, None


def src_spotify(artist, title):
    import base64
    cid, sec = _spotify_creds()
    if not cid or not sec:
        return None  # not configured → silently skipped
    if _spotify_token["exp"] < time.time():
        auth = base64.b64encode(f"{cid}:{sec}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request("https://accounts.spotify.com/api/token", data=body,
                                     headers={"Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                t = json.loads(r.read().decode())
            _spotify_token.update(value=t["access_token"], exp=time.time() + t.get("expires_in", 3600) - 60)
        except Exception:
            return None
    hdr = {"Authorization": f"Bearer {_spotify_token['value']}"}
    q = urllib.parse.quote(f"{title} artist:{artist}")
    d = http_json(f"https://api.spotify.com/v1/search?q={q}&type=track&limit=5", headers=hdr,
                  host="spotify", interval=0.2)
    if not d:
        return None
    KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    for r in d.get("tracks", {}).get("items", []):
        if _title_matches(title, r.get("name", "")):
            f = http_json(f"https://api.spotify.com/v1/audio-features/{r['id']}", headers=hdr,
                          host="spotify", interval=0.2)
            if not f:
                return {}
            key = None
            if isinstance(f.get("key"), int) and f["key"] >= 0:
                key = KEYS[f["key"]] + (" Major" if f.get("mode") == 1 else " Minor")
            return {"bpm": round(f.get("tempo") or 0), "key": key}
    return {}


# ---------------------------------------------------------------- helpers
def _dedup_names(names):
    seen, out = set(), []
    for n in names:
        n = (n or "").strip()
        k = n.lower()
        if k and k not in seen and k not in PRODUCER_JUNK:
            seen.add(k)
            out.append(n)
    return out


# ---------------------------------------------------------------- reconciliation
def reconcile(existing, sources, track_artist=""):
    """existing: {bpm,key,producers,format}; sources: {name: dict|None}. Returns the
    chosen values + per-field provenance + conflict flags."""
    out = {"fields": {}}

    # ---- release format: clash all sources, but only a REAL album BY THE TRACK'S OWN
    # ARTIST can name the album. Compilations / soundtracks / playlists / "Various
    # Artists" releases are rejected (a track whose only "albums" are comps is a Single,
    # not an album track). Existing curated "Album: X" entries are protected — only a
    # DIFFERENT strong real album overrides them, never a downgrade to a weak guess. ----
    real_names = Counter()       # artist-matched, non-comp album titles -> weight
    real_display = {}
    single_votes = 0
    saw_comp = False
    for src_name in ("itunes", "deezer", "genius", "musicbrainz"):
        s = sources.get(src_name) or {}
        alb = s.get("album")
        t = (s.get("type") or "")
        nt = s.get("ntracks")
        mb_comp = bool(set(s.get("secondary") or []) &
                       {"compilation", "soundtrack", "mixtape/street", "dj-mix", "live", "demo"})
        if t == "single" and not (nt and nt > 1):
            single_votes += 1            # a true 1-track single — not album evidence
            continue
        if not alb:
            continue
        # Reject comps, different-artist releases, and mega-track playlists (>30 tracks
        # is almost always a playlist/compilation — "Just Dance 2017", "Discoradio
        # Compilation" — not the track's own album).
        if mb_comp or (nt and nt > 30) or not _artist_match(s.get("album_artist"), track_artist):
            saw_comp = True
            continue
        key = _norm(alb)
        if not key:
            continue
        w = 2.0 if (t in ("album", "ep") and (nt is None or nt > 1)) else 1.5
        real_names[key] += w
        real_display.setdefault(key, _clean_album(alb))
    # "strong" = agreement on ONE album: a full-album source (w=2.0), OR 2+ sources
    # naming the SAME album (1.5+1.5). NOT a sum across DIFFERENT names — that let a
    # remix-EP + a playlist masquerade as strong and overwrite curated album data.
    strong = bool(real_names) and max(real_names.values()) >= 2.0
    best_album = (f"Album: {real_display[max(real_names.items(), key=lambda kv: kv[1])[0]]}"
                  if real_names else None)

    existing_fmt = existing.get("format")
    chosen_fmt, fmt_conflict = existing_fmt, False
    if (existing_fmt or "").startswith("Album"):
        # Protect curated album data: override only with a DIFFERENT strong real album.
        if strong and best_album and _norm(best_album) != _norm(existing_fmt):
            chosen_fmt, fmt_conflict = best_album, True
    else:
        # existing is Single / None — the unreliable default; apply the clash freely.
        if best_album:
            chosen_fmt = best_album
            fmt_conflict = single_votes > 0 or len(real_names) > 1
        elif single_votes > 0 or saw_comp:
            chosen_fmt = "Single"        # explicit single, or comp/playlist-only appearances
            fmt_conflict = bool(real_names)
    out["fields"]["format"] = {
        "chosen": chosen_fmt, "existing": existing_fmt,
        "album_votes": {real_display.get(k, k): round(v, 1) for k, v in real_names.items()},
        "single_votes": single_votes, "saw_comp": saw_comp,
        "changed": bool(chosen_fmt) and chosen_fmt != existing_fmt,
        "conflict": fmt_conflict,
    }

    # ---- bpm (librosa vs Deezer vs Spotify; fix octave errors) ----
    ex_bpm = existing.get("bpm") or 0
    api_bpm = 0
    for name in ("spotify", "deezer"):  # spotify first (most authoritative tempo)
        v = (sources.get(name) or {}).get("bpm") or 0
        if v:
            api_bpm = round(v)
            break
    chosen_bpm, bpm_note, bpm_conflict = ex_bpm, None, False
    if api_bpm:
        if not ex_bpm or abs(ex_bpm - api_bpm) <= 2:
            chosen_bpm = api_bpm if not ex_bpm else ex_bpm
        elif abs(ex_bpm - 2 * api_bpm) <= 3 or abs(ex_bpm - api_bpm / 2) <= 3:
            chosen_bpm, bpm_note = api_bpm, "octave-fix"  # librosa half/double-time error
        else:
            chosen_bpm, bpm_note, bpm_conflict = api_bpm, "disagree", True
    out["fields"]["bpm"] = {
        "chosen": chosen_bpm, "existing": ex_bpm, "api": api_bpm, "note": bpm_note,
        "changed": chosen_bpm != ex_bpm, "conflict": bpm_conflict,
    }

    # ---- key (Spotify if available, else keep librosa) ----
    sp_key = (sources.get("spotify") or {}).get("key")
    chosen_key = sp_key or existing.get("key")
    out["fields"]["key"] = {
        "chosen": chosen_key, "existing": existing.get("key"), "spotify": sp_key,
        "changed": bool(sp_key) and sp_key != existing.get("key"),
        "conflict": bool(sp_key) and bool(existing.get("key")) and sp_key != existing.get("key"),
    }

    # ---- producers (union across sources) ----
    pset = list(existing.get("producers") or [])
    for name in ("genius", "musicbrainz"):
        pset += (sources.get(name) or {}).get("producers") or []
    merged = _dedup_names(pset)
    out["fields"]["producers"] = {
        "chosen": merged, "existing": existing.get("producers") or [],
        "changed": merged != (existing.get("producers") or []),
        "conflict": False,
    }

    # ---- year (earliest plausible across sources) ----
    years = [s.get("year") for s in sources.values() if s and s.get("year")]
    years = [y for y in years if 1900 < y <= datetime.now().year]
    chosen_year = min(years) if years else None
    out["fields"]["year"] = {"chosen": chosen_year, "changed": chosen_year is not None, "conflict": False}

    # ---- label (MusicBrainz preferred) ----
    label = (sources.get("musicbrainz") or {}).get("label")
    out["fields"]["label"] = {"chosen": label, "changed": bool(label), "conflict": False}

    return out


# ---------------------------------------------------------------- catalog IO
def load_catalog():
    songs = json.load(open(INDEX))["songsList"]
    formats = json.load(open(RELEASE)) if os.path.exists(RELEASE) else {}
    return songs, formats


def song_path(sid):
    return os.path.join(SONGS, f"{int(sid):04d}.json")


def existing_for(song, formats):
    detail = {}
    fp = song_path(song["id"])
    if os.path.exists(fp):
        try:
            detail = json.load(open(fp))
        except Exception:
            detail = {}
    prod = detail.get("producers")
    plist = _dedup_names(re.split(r"[;,]", prod)) if isinstance(prod, str) else _dedup_names(prod or [])
    return {
        "bpm": song.get("bpm") or detail.get("bpm") or 0,
        "key": song.get("key") or detail.get("key"),
        "producers": plist,
        "format": formats.get(str(song["id"])),
    }, detail


# ---------------------------------------------------------------- cache
class Cache:
    def __init__(self, path):
        self.path = path
        self.lock = Lock()
        self.data = {}
        if os.path.exists(path):
            try:
                self.data = json.load(open(path))
            except Exception:
                self.data = {}

    def get(self, key):
        with self.lock:
            return self.data.get(key)

    def put(self, key, val):
        with self.lock:
            self.data[key] = val

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            json.dump(self.data, open(tmp, "w"), ensure_ascii=False)
            os.replace(tmp, self.path)


SOURCE_FNS = {"itunes": src_itunes, "deezer": src_deezer, "genius": src_genius,
              "musicbrainz": src_musicbrainz, "spotify": src_spotify}


def query_all(artist, title, cache, skip_mb=False):
    out = {}
    for name, fn in SOURCE_FNS.items():
        ck = f"{name}|{artist} - {title}"
        cached = cache.get(ck)
        if cached is not None:
            out[name] = cached if cached != "__none__" else None
            continue
        if name == "musicbrainz" and skip_mb:
            out[name] = None  # use cached MB if present (above), but don't pay 1 req/s for new ones
            continue
        res = fn(artist, title)
        cache.put(ck, res if res is not None else "__none__")
        out[name] = res
    return out


# ---------------------------------------------------------------- apply
def apply_changes(song, detail, rec, formats_out):
    """Write reconciled fields. Release format → `formats_out` (release-formats.json).
    producers / bpm / key → the per-song JSON, but ONLY when something actually changed,
    so the CDN upload pushes real diffs rather than re-serialising all 1364 files. Year/
    label are computed in the report but intentionally NOT written (not requested)."""
    sid = str(song["id"])
    fields = rec["fields"]
    if fields["format"]["changed"] and fields["format"]["chosen"]:
        formats_out[sid] = fields["format"]["chosen"]
    dirty = False
    if fields["bpm"]["changed"] and fields["bpm"]["chosen"]:
        song["bpm"] = fields["bpm"]["chosen"]
        detail["bpm"] = fields["bpm"]["chosen"]
        dirty = True
    if fields["key"]["changed"] and fields["key"]["chosen"]:
        song["key"] = fields["key"]["chosen"]
        detail["key"] = fields["key"]["chosen"]
        dirty = True
    if fields["producers"]["changed"] and fields["producers"]["chosen"]:
        detail["producers"] = ", ".join(fields["producers"]["chosen"])
        dirty = True
    if dirty:
        fp = song_path(song["id"])
        if os.path.exists(fp):
            json.dump(detail, open(fp, "w"), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write corrections (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", type=str, default="")
    ap.add_argument("--match", type=str, default="")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-mb", action="store_true",
                    help="skip NEW MusicBrainz fetches (still uses cached MB); much faster "
                         "full run since MB's 1 req/s is the bottleneck. iTunes+Deezer+Genius "
                         "already cover album/single/producers/year.")
    args = ap.parse_args()

    songs, formats = load_catalog()
    if args.ids:
        want = set(args.ids.split(","))
        songs = [s for s in songs if str(s["id"]) in want]
    if args.match:
        songs = [s for s in songs if args.match.lower() in (s.get("title", "").lower())]
    if args.limit:
        songs = songs[:args.limit]

    print(f"{'APPLY' if args.apply else 'DRY-RUN'} over {len(songs)} tracks. "
          f"Spotify {'ON' if _spotify_creds()[0] else 'off (no creds → BPM via Deezer, key via librosa)'}.")
    if args.apply:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        # Outside the app repo: a copy beside musicdata sat in the app's Resources.
        backup_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
        os.makedirs(backup_root, exist_ok=True)
        backup = os.path.join(backup_root, f"musicdata.bak-{ts}")
        shutil.copytree(APP, backup)
        print(f"Backed up catalogue → {backup}  (revert: rm -rf {APP} && mv {backup} {APP})")
    cache = Cache(CACHE_PATH)
    results = []
    counts = Counter()
    lock = Lock()

    def work(song):
        artist, title = song.get("artist", ""), song.get("title", "")
        existing, detail = existing_for(song, formats)
        srcs = query_all(artist, title, cache, skip_mb=args.no_mb)
        rec = reconcile(existing, srcs, track_artist=artist)
        return song, detail, existing, srcs, rec

    formats_out = dict(formats)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(work, s): s for s in songs}
        for fut in as_completed(futs):
            try:
                song, detail, existing, srcs, rec = fut.result()
            except Exception as e:
                print("  ! error:", e)
                continue
            done += 1
            f = rec["fields"]
            for fld in ("format", "bpm", "key", "producers"):
                if f[fld]["changed"]:
                    counts[f"{fld}_changed"] += 1
                if f[fld]["conflict"]:
                    counts[f"{fld}_conflict"] += 1
            results.append({"id": song["id"], "title": song.get("title"),
                            "artist": song.get("artist"), "existing": existing,
                            "sources": srcs, "reconciled": rec})
            if args.apply:
                with lock:
                    apply_changes(song, detail, rec, formats_out)
            if done % 25 == 0:
                cache.save()
                print(f"  {done}/{len(songs)}  changes: {dict(counts)}")
    cache.save()

    # write reports
    json.dump({"generated": datetime.now().isoformat(), "counts": dict(counts),
               "results": results}, open(REPORT_JSON, "w"), ensure_ascii=False, indent=1)
    write_md_report(results, counts, len(songs))

    if args.apply:
        json.dump(formats_out, open(RELEASE, "w"), ensure_ascii=False, indent=2)
        # rewrite music-index with any bpm/key changes
        idx = json.load(open(INDEX))
        by_id = {str(s["id"]): s for s in results_index(results)}
        for s in idx["songsList"]:
            u = by_id.get(str(s["id"]))
            if u:
                s.update(u)
        json.dump(idx, open(INDEX, "w"), ensure_ascii=False)
        print("APPLIED. Re-publish with ios-zingy/scripts/upload-catalog.sh")

    print(f"\nDone. {len(songs)} tracks. Changes: {dict(counts)}")
    print(f"Report: {REPORT_MD}")


def results_index(results):
    """music-index updates (bpm/key) per track that changed."""
    out = []
    for r in results:
        upd = {"id": r["id"]}
        f = r["reconciled"]["fields"]
        if f["bpm"]["changed"] and f["bpm"]["chosen"]:
            upd["bpm"] = f["bpm"]["chosen"]
        if f["key"]["changed"] and f["key"]["chosen"]:
            upd["key"] = f["key"]["chosen"]
        if len(upd) > 1:
            out.append(upd)
    return out


def write_md_report(results, counts, total):
    lines = [f"# Metadata reconciliation report", "",
             f"Generated {datetime.now():%Y-%m-%d %H:%M} over **{total}** tracks.", "",
             "## Summary", ""]
    for k in ("format_changed", "bpm_changed", "key_changed", "producers_changed",
              "format_conflict", "bpm_conflict", "key_conflict"):
        lines.append(f"- {k}: **{counts.get(k, 0)}**")
    lines += ["", "## Per-track changes & conflicts", ""]
    for r in results:
        f = r["reconciled"]["fields"]
        changed = [k for k in ("format", "bpm", "key", "producers") if f[k]["changed"]]
        conflicts = [k for k in ("format", "bpm", "key") if f[k]["conflict"]]
        if not changed and not conflicts:
            continue
        lines.append(f"### [{r['id']}] {r['artist']} — {r['title']}")
        if f["format"]["changed"]:
            lines.append(f"- format: `{f['format']['existing']}` → **{f['format']['chosen']}** "
                         f"(album votes: {f['format']['album_votes']}, "
                         f"single votes: {f['format']['single_votes']})"
                         + ("  ⚠️CONFLICT" if f["format"]["conflict"] else ""))
        if f["bpm"]["changed"]:
            lines.append(f"- bpm: `{f['bpm']['existing']}` → **{f['bpm']['chosen']}** "
                         f"(api {f['bpm']['api']}, {f['bpm']['note']})"
                         + ("  ⚠️CONFLICT" if f["bpm"]["conflict"] else ""))
        if f["key"]["changed"]:
            lines.append(f"- key: `{f['key']['existing']}` → **{f['key']['chosen']}**")
        if f["producers"]["changed"]:
            lines.append(f"- producers: `{f['producers']['existing']}` → **{f['producers']['chosen']}**")
        lines.append("")
    open(REPORT_MD, "w").write("\n".join(lines))


if __name__ == "__main__":
    main()
