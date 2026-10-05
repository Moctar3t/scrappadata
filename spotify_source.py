#!/usr/bin/env python3
"""Spotify as a metadata source, alongside Genius/Deezer/iTunes in meta_sources.py.

Scope note (verified 2026-08-05 against the `scrappadata` app):
  * /v1/search            → OK
  * /v1/tracks/{id}       → OK  (popularity, disc/track no, explicit)
  * /v1/albums/{id}       → OK  (label, release_date, total_tracks, album_type)
  * /v1/audio-features    → 403 FORBIDDEN. Spotify cut this endpoint off for apps
                            created after Nov 2024, so danceability/energy/valence
                            CANNOT come from here. Those still have to come from
                            the Exportify CSV path (parse_spotify_csv.py) or stay
                            as-is. Don't waste calls retrying it.

Everything is validated before it's trusted — taking hits[0] blindly is how
wrong-song metadata got into the catalogue in the first place.
"""
from __future__ import annotations
import base64, json, os, re, time, unicodedata, urllib.parse, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "spotify_cache.json")

# The keys live in .spotify.json (private, never committed), or the environment.
try:
    with open(os.path.join(HERE, ".spotify.json"), encoding="utf-8") as _f:
        _KEYS = json.load(_f)
except FileNotFoundError:
    _KEYS = {}
CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID") or _KEYS.get("client_id", "")
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET") or _KEYS.get("client_secret", "")

_token = None
_token_exp = 0.0


# ── auth ───────────────────────────────────────────────────────────────────
def token() -> str:
    global _token, _token_exp
    if _token and time.time() < _token_exp - 60:
        return _token
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token", data=body,
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    _token = d["access_token"]
    _token_exp = time.time() + d.get("expires_in", 3600)
    return _token


# ── disk cache (hits AND misses — a miss is expensive to re-derive) ────────
_cache = None
_dirty = 0


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.load(open(CACHE_PATH, encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


def flush_cache() -> None:
    global _dirty
    if _cache is not None and _dirty:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
        _dirty = 0


def _cache_put(key: str, val) -> None:
    global _dirty
    _load_cache()[key] = val
    _dirty += 1
    if _dirty >= 100:
        flush_cache()


# ── HTTP with 429 handling ─────────────────────────────────────────────────
class TransientError(Exception):
    """The API did not answer. Distinct from 'answered, nothing there' — callers
    MUST NOT cache this as a miss, or a rate-limit blip permanently records real
    songs as 'not on Spotify' (which is exactly what happened on the first run:
    a 429 storm poisoned every entry to null and the sweep reported 0% matched)."""


# Politeness throttle. Spotify's client-credentials budget is a rolling window and
# a burst gets the whole app parked for minutes, so pace requests rather than
# sprinting into a wall and backing off.
_MIN_INTERVAL = 0.22          # ≈4.5 req/s
_last_call = 0.0


def _throttle():
    global _last_call
    gap = time.time() - _last_call
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)
    _last_call = time.time()


def _get(path: str, params: dict | None = None, tries: int = 5):
    url = "https://api.spotify.com/v1/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(tries):
        _throttle()
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "5")) + 1
                time.sleep(min(wait, 120))
                continue
            if e.code == 401:
                global _token
                _token = None
                continue
            if e.code == 404:
                return None                      # genuinely absent
            if e.code == 403:
                raise TransientError(f"403 on {path}")   # scope/deprecation, not a miss
            if attempt == tries - 1:
                raise TransientError(f"HTTP {e.code} on {path}")
            time.sleep(1.5 * (attempt + 1))
        except urllib.error.URLError as e:
            if attempt == tries - 1:
                raise TransientError(str(e))
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:
            if attempt == tries - 1:
                raise TransientError(str(e))
            time.sleep(1.5 * (attempt + 1))
    raise TransientError(f"exhausted retries on {path}")


# ── matching / validation ──────────────────────────────────────────────────
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\((feat|ft|with)[^)]*\)", " ", s)
    s = re.sub(r"\[(feat|ft|with)[^\]]*\]", " ", s)
    s = re.sub(r"\b(official|video|audio|remaster(ed)?|extended|version|edit|radio)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _tokens(s: str) -> set:
    return set(_norm(s).split())


# Edition qualifiers. "A Night At The Opera (2011 Remaster)" and "A Night At The
# Opera (Deluxe Remastered)" are the SAME record — comparing them raw reports a
# mismatch that isn't one, which is most of what a naive album diff turns up.
_EDITION = re.compile(
    r"\((?:[^)]*\b(?:remaster|remix|deluxe|edition|version|expanded|anniversary|reissue|"
    r"bonus|explicit|clean|mono|stereo|live|hosted|re ?up|instrumental|super|special|"
    r"collector|platinum|gold|complete|extended|original|motion picture|soundtrack)\b[^)]*)\)"
    r"|\[[^\]]*\b(?:remaster|remix|deluxe|edition|version|hosted|re ?up)\b[^\]]*\]",
    re.I)


def norm_album(s: str) -> str:
    s = _EDITION.sub(" ", s or "")
    stripped = re.sub(r"\b(19|20)\d{2}\b", " ", s)   # stray "2011 Remaster" years
    out = _norm(stripped)
    if out:
        return out
    # A title that IS a bare year ("2024", "1942") strips to empty, and
    # album_same() treats an empty side as "nothing to disagree about" — i.e. a
    # match against ANYTHING. That falsely merged Dcxshy's "2024" with
    # MusicBrainz's unrelated "Better Off Alone". Fall back to the unstripped
    # normalisation so a numeric title still matches only itself.
    return _norm(s)


def album_same(a: str, b: str) -> bool:
    """True when two album titles denote the same record."""
    na, nb = norm_album(a), norm_album(b)
    if not na or not nb:
        return True                                   # nothing to disagree about
    if na == nb:
        return True
    # One is a prefix/superset of the other ("Pink Friday" vs "Pink Friday: Reloaded")
    if na in nb or nb in na:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return len(ta & tb) / max(1, min(len(ta), len(tb))) >= 0.8


def _artist_tokens(artist: str) -> set:
    """App stores collaborators separated by ';' or ','."""
    parts = re.split(r"[;,&]| and ", artist or "")
    out = set()
    for p in parts:
        out |= _tokens(p)
    return out - {"the", "a", "of"}


def is_match(want_title: str, want_artist: str, hit: dict) -> bool:
    """Title must overlap strongly AND at least one artist token must agree."""
    ht = _tokens(hit.get("name", ""))
    wt = _tokens(want_title)
    if not ht or not wt:
        return False
    overlap = len(ht & wt) / max(1, len(wt))
    if overlap < 0.6:
        return False
    ha = set()
    for a in hit.get("artists", []):
        ha |= _tokens(a.get("name", ""))
    wa = _artist_tokens(want_artist)
    if not wa:
        return True
    return bool(ha & wa)


# ── public lookups ─────────────────────────────────────────────────────────
def find_track(title: str, artist: str) -> dict | None:
    """Best validated Spotify track for (title, artist), or None."""
    key = "t|" + _norm(title) + "|" + _norm(artist)
    c = _load_cache()
    if key in c:
        return c[key]

    primary = re.split(r"[;,&]", artist or "")[0].strip()
    queries = [
        f'track:"{title}" artist:"{primary}"',
        f"{title} {primary}",
    ]
    best = None
    all_valid = []
    answered = False
    for q in queries:
        d = _get("search", {"q": q, "type": "track", "limit": 10, "market": "US"})
        answered = True                      # _get raises TransientError otherwise
        items = ((d or {}).get("tracks") or {}).get("items") or []
        for it in items:
            if is_match(title, artist, it):
                all_valid.append(it)
                if best is None:
                    best = it
        if best:
            break

    if not best:
        # Only record a miss if Spotify actually replied and had nothing.
        if answered:
            _cache_put(key, None)
        return None

    # EARLIEST release across every validated pressing. Spotify's top hit for an
    # old song is very often a remaster or compilation, whose release_date is the
    # REISSUE year — treating that as the song's year manufactures "errors" and,
    # worse, would overwrite correct data with a reissue date. The minimum across
    # versions is a far better estimate of when the song actually came out.
    years = []
    for it in all_valid:
        m = re.match(r"(\d{4})", ((it.get("album") or {}).get("release_date") or ""))
        if m:
            years.append(int(m.group(1)))
    earliest = min(years) if years else None

    alb = best.get("album") or {}
    out = {
        "id": best.get("id"),
        "name": best.get("name"),
        "artists": [a.get("name") for a in best.get("artists", [])],
        "album": alb.get("name"),
        "album_id": alb.get("id"),
        "album_type": alb.get("album_type"),
        "release_date": alb.get("release_date"),
        "total_tracks": alb.get("total_tracks"),
        "images": [i.get("url") for i in (alb.get("images") or [])],
        "popularity": best.get("popularity"),
        "explicit": best.get("explicit"),
        "isrc": ((best.get("external_ids") or {}).get("isrc")),
        "earliest_year": earliest,
        "versions_seen": len(all_valid),
    }
    _cache_put(key, out)
    return out


def album_details(album_id: str) -> dict | None:
    """Label + precise release info live on the album object, not the track."""
    if not album_id:
        return None
    key = "a|" + album_id
    c = _load_cache()
    if key in c:
        return c[key]
    try:
        d = _get(f"albums/{album_id}")
    except TransientError:
        return None                          # don't poison the cache
    if not d:
        _cache_put(key, None)
        return None
    out = {
        "label": d.get("label"),
        "name": d.get("name"),
        "album_type": d.get("album_type"),
        "release_date": d.get("release_date"),
        "total_tracks": d.get("total_tracks"),
        "genres": d.get("genres") or [],
    }
    _cache_put(key, out)
    return out
