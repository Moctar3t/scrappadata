#!/usr/bin/env python3
"""Shared metadata sources for the catalogue pipeline — the ONE place that talks
to Genius / Deezer / iTunes. Everything else (local_metadata_enricher.py,
polish_catalog.py, one-off fixes) imports from here.

Design rules learned the hard way:
  * Genius = the OFFICIAL api.genius.com with the bearer token from .genius_token.
    The unofficial genius.com/api endpoint IP-bans and silently returns nothing —
    that is why 85% of the catalogue ended up with no producers.
  * Every hit is VALIDATED (title + artist token overlap) before we trust it.
    Taking hits[0] blindly is how wrong-song metadata happened.
  * Hits are cached on disk (meta_cache.json) so re-runs are instant and free.
  * iTunes is last-resort only (hard 20 req/min rate limit) — used when year or
    label is still missing after Genius + Deezer.

Field sources (primary → fallback):
  producers, writers, featuring, year : Genius → (year) Deezer → iTunes
  label                               : Genius custom_performances → Deezer album → iTunes ℗ line
  cover art                           : Genius song_art (primary) — caller keeps the
                                        old (Spotify/Apple) cover as the fallback
  album, release_type                 : Deezer album → iTunes collection
"""
from __future__ import annotations
import json, os, re, time, unicodedata, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "meta_cache.json")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}


def genius_token() -> str:
    tok = os.environ.get("GENIUS_TOKEN")
    if tok:
        return tok.strip()
    p = os.path.join(HERE, ".genius_token")
    if os.path.exists(p):
        return open(p).read().strip()
    raise SystemExit("no Genius token: set GENIUS_TOKEN or create scrappadata/.genius_token")


# ── tiny disk cache (hits only — misses stay retryable) ─────────────────────
_cache: dict | None = None
_cache_dirty = 0


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.load(open(CACHE_PATH, encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


def _cache_put(key: str, val: dict) -> None:
    global _cache_dirty
    _load_cache()[key] = val
    _cache_dirty += 1
    if _cache_dirty >= 25:
        save_cache()


def save_cache() -> None:
    global _cache_dirty
    if _cache is not None:
        tmp = CACHE_PATH + ".tmp"
        json.dump(_cache, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
        _cache_dirty = 0


# ── normalisation + match validation ────────────────────────────────────────
def norm(s: str) -> str:
    """Lowercase, strip feat-clauses and punctuation. \\w keeps CJK/kana intact
    so Japanese titles still compare (an ascii-only regex nukes them to '')."""
    s = unicodedata.normalize("NFKC", (s or "").lower())
    s = re.sub(r"\(feat[^)]*\)|\[feat[^\]]*\]|\(with[^)]*\)|\bfeat\.?\b.*|\bft\.?\b.*", "", s)
    return re.sub(r"[^\w]+", "", s, flags=re.UNICODE)


def primary_artist(artist: str) -> str:
    return re.split(r"[;,]|\bfeat\.?\b|\bft\.?\b|\bavec\b", artist or "", flags=re.I)[0].strip()


def clean_title_for_query(title: str) -> str:
    """Remove common YouTube/leak metadata junk from the query title.
    Leaves the original title intact for matching verification in _titles_match."""
    t = re.sub(r"\(feat[^)]*\)|\[feat[^\]]*\]|\(with[^)]*\)|\bfeat\.?\b.*|\bft\.?\b.*", "", title, flags=re.I)
    pat = r"\b(visualizer|official video|music video|official audio|audio|video clip|lyric video|lyrics|clip officiel|clip|official|officiel)\b"
    t = re.sub(pat, "", t, flags=re.I)
    t = re.sub(r"\b(sped up|slowed|slowed \+ reverb|slowed & reverb)\b.*", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t)
    return t.strip(" -–—| ")


def artist_tokens(artist: str) -> set:
    parts = re.split(r"[;,&]|\bfeat\.?\b|\bft\.?\b|\band\b|\bx\b|/", (artist or "").lower())
    return {norm(p) for p in parts if norm(p)}


def _titles_match(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    return bool(na and nb) and (na == nb or na in nb or nb in na)


def _artists_match(want: str, got: str) -> bool:
    w, g = artist_tokens(want), artist_tokens(got)
    if not w or not g:
        return False
    return any(x and y and (x in y or y in x) for x in w for y in g)


def _get_json(url: str, headers: dict | None = None, timeout: int = 12):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _is_real_art(url: str | None) -> bool:
    return bool(url) and "default_cover_image" not in url and "default_avatar" not in url


# ── GENIUS (official API) ────────────────────────────────────────────────────
def genius_song(artist: str, title: str) -> dict | None:
    """→ {producers, writers, featuring, year, release_date, label, art, url} or None.
    Values are None when Genius doesn't know them — never guessed."""
    key = f"genius|{norm(artist)}|{norm(title)}"
    if key in _load_cache():
        return _load_cache()[key]
    tok = genius_token()
    hdrs = {"Authorization": f"Bearer {tok}"}
    t_clean = clean_title_for_query(title)
    queries = [f"{primary_artist(artist)} {t_clean}"]
    if primary_artist(artist) != (artist or "").strip():
        queries.append(f"{artist} {t_clean}")
    if t_clean != title:
        queries.append(f"{primary_artist(artist)} {title}")
    best = None
    for q in queries:
        try:
            d = _get_json("https://api.genius.com/search?q=" + urllib.parse.quote(q), hdrs)
        except Exception as e:
            print(f"    genius search error: {e}")
            return None                      # transient — stay retryable
        for h in d.get("response", {}).get("hits", [])[:6]:
            r = h.get("result", {})
            if _titles_match(title, r.get("title", "")) and \
               _artists_match(artist, r.get("artist_names") or r.get("primary_artist", {}).get("name", "")):
                best = r
                break
        if best:
            break
        time.sleep(0.15)
    if not best:
        return None
    time.sleep(0.15)
    try:
        song = _get_json(f"https://api.genius.com/songs/{best['id']}?text_format=plain",
                         hdrs).get("response", {}).get("song", {})
    except Exception as e:
        print(f"    genius song error: {e}")
        return None

    producers = [p["name"] for p in song.get("producer_artists", [])]
    prod_roles = {"producer", "co-producer", "additional production", "additional producer"}
    label = None
    for perf in song.get("custom_performances", []):
        role = (perf.get("label") or "").strip().lower()
        if role in prod_roles:
            producers += [a["name"] for a in perf.get("artists", [])]
        elif role == "label" and perf.get("artists"):
            label = perf["artists"][0]["name"]
    seen, prods = set(), []
    for p in producers:
        k = p.strip().lower()
        if k and k not in seen and k not in {"self-produced", "n/a", "none"}:
            seen.add(k)
            prods.append(p.strip())
    writers = [w["name"] for w in song.get("writer_artists", [])]
    year = (song.get("release_date_components") or {}).get("year")
    art = song.get("song_art_image_url")
    out = {
        "producers": "; ".join(prods) or None,
        "writers": "; ".join(dict.fromkeys(writers)) or None,
        "featuring": ", ".join(f["name"] for f in song.get("featured_artists", [])) or None,
        "year": year,
        "release_date": song.get("release_date"),
        "label": label,
        "art": art if _is_real_art(art) else None,
        "url": song.get("url"),
    }
    _cache_put(key, out)
    return out


# ── DEEZER ───────────────────────────────────────────────────────────────────
def deezer_info(artist: str, title: str) -> dict | None:
    """→ {label, year, release_date, album, release_type, art, genres} or None."""
    key = f"deezer|{norm(artist)}|{norm(title)}"
    if key in _load_cache():
        return _load_cache()[key]
    a = primary_artist(artist)
    t_clean = clean_title_for_query(title)
    matches = []
    t_vals = [t_clean]
    if t_clean != title:
        t_vals.append(re.sub(r"\(feat[^)]*\)", "", title, flags=re.I).strip())
    queries = []
    for tv in t_vals:
        queries.extend([f'track:"{tv}" artist:"{a}"', f"{a} {tv}"])
    for q in queries:
        try:
            d = _get_json("https://api.deezer.com/search?limit=5&q=" + urllib.parse.quote(q))
        except Exception as e:
            print(f"    deezer error: {e}")
            return None
        matches = [c for c in d.get("data", [])
                   if _titles_match(title, c.get("title", "")) and
                   _artists_match(artist, c.get("artist", {}).get("name", "")) and
                   c.get("album", {}).get("id")]
        if matches:
            break
        time.sleep(0.15)
    if not matches:
        return None
    # Prefer a non-compilation album (the original release), checking up to 3.
    al = None
    for cand in matches[:3]:
        time.sleep(0.15)
        try:
            a2 = _get_json(f"https://api.deezer.com/album/{cand['album']['id']}")
        except Exception:
            continue
        if al is None:
            al = a2
        if (a2.get("record_type") or "").lower() != "compilation":
            al = a2
            break
    if al is None:
        return None
    rd = al.get("release_date")
    out = {
        "label": (al.get("label") or "").strip() or None,
        "year": int(rd[:4]) if rd and re.match(r"^\d{4}", rd) else None,
        "release_date": rd,
        "album": al.get("title"),
        "release_type": al.get("record_type"),
        "art": al.get("cover_xl") or al.get("cover_big"),
        "genres": [g["name"] for g in al.get("genres", {}).get("data", [])] or None,
    }
    _cache_put(key, out)
    return out


# ── MUSICBRAINZ (the authority on ORIGINAL release year + label) ────────────
# iTunes/Deezer carry the *digital reissue* date for older songs (a 1983 city pop
# track shows 2017 there). MusicBrainz recordings carry `first-release-date` and
# artist aliases across scripts (山口美央子 ⇔ Mioko Yamaguchi), so it both finds
# the song AND dates it correctly. Rate limit: 1 req/s — we sleep 1.1s per call.
MB_UA = {"User-Agent": "ZingyCatalog/1.0 (dimemoctar@gmail.com)"}


def _mb_artist_id(artist: str) -> str | None:
    """Resolve an artist to a MusicBrainz MBID. The artist index searches ALIASES,
    which is what lets 'Mioko Yamaguchi' resolve to 山口美央子. Cached per artist,
    so a whole discography costs one lookup."""
    a = primary_artist(artist)
    key = f"mbartist|{norm(a)}"
    if key in _load_cache():
        return _load_cache()[key].get("id")
    try:
        d = _get_json("https://musicbrainz.org/ws/2/artist?fmt=json&limit=3&query="
                      + urllib.parse.quote(f'artist:"{a}" OR alias:"{a}"'), MB_UA)
    except Exception:
        return None
    time.sleep(1.1)
    arts = d.get("artists", [])
    if arts and int(arts[0].get("score", 0)) >= 95:
        aliases = [al.get("name") for al in arts[0].get("aliases", []) if al.get("name")]
        _cache_put(key, {"id": arts[0]["id"], "name": arts[0].get("name"), "aliases": aliases})
        return arts[0]["id"]
    return None


def artist_aliases(artist: str) -> set[str]:
    """All known names for an artist across scripts (竹内まりや ⇔ Mariya Takeuchi),
    via MusicBrainz aliases. Includes the queried name itself; cached per artist,
    so a whole discography costs one lookup. Used to make the ingest gate's
    official-source check work on Japanese/Korean/etc. catalogues."""
    a = primary_artist(artist)
    names = {a}
    if _mb_artist_id(a):                       # populates the cache
        e = _load_cache().get(f"mbartist|{norm(a)}", {})
        names |= {e.get("name") or a} | set(e.get("aliases") or [])
    return {n for n in names if n}


def musicbrainz_info(artist: str, title: str, want_label: bool = True) -> dict | None:
    """→ {year, release_date, label, album} from the EARLIEST known release."""
    key = f"mb|{norm(artist)}|{norm(title)}"
    if key in _load_cache():
        return _load_cache()[key]
    arid = _mb_artist_id(artist)
    t_clean = clean_title_for_query(title)
    q = f'recording:"{t_clean}" AND arid:{arid}' if arid else \
        f'recording:"{t_clean}" AND artist:"{primary_artist(artist)}"'
    try:
        d = _get_json("https://musicbrainz.org/ws/2/recording?fmt=json&limit=8&query="
                      + urllib.parse.quote(q), MB_UA)
    except Exception as e:
        print(f"    musicbrainz error: {e}")
        return None
    time.sleep(1.1)
    recs = [r for r in d.get("recordings", [])
            if int(r.get("score", 0)) >= 90 and _titles_match(title, r.get("title", ""))]
    # Exact-title matches beat variants ("new ver.", "remix") that merely contain it.
    exact = [r for r in recs if norm(r.get("title", "")) == norm(title)]
    recs = exact or recs
    if not recs:
        return None
    # earliest first-release-date across matching recordings
    dated = [r for r in recs if r.get("first-release-date")]
    dated.sort(key=lambda r: r["first-release-date"])
    if not dated:
        return None
    best = dated[0]
    rd = best["first-release-date"]
    year = int(rd[:4]) if re.match(r"^\d{4}", rd) else None
    label = album = None
    releases = sorted([x for x in best.get("releases", []) if x.get("date")],
                      key=lambda x: x["date"]) or best.get("releases", [])
    if releases:
        album = releases[0].get("title")
        if want_label and releases[0].get("id"):
            try:
                rel = _get_json(f"https://musicbrainz.org/ws/2/release/{releases[0]['id']}"
                                "?fmt=json&inc=labels", MB_UA)
                li = rel.get("label-info", [])
                if li and li[0].get("label", {}).get("name"):
                    label = li[0]["label"]["name"]
            except Exception:
                pass
            time.sleep(1.1)
    out = {"year": year, "release_date": rd if len(rd) >= 7 else None, "label": label, "album": album}
    _cache_put(key, out)
    return out


# ── iTUNES (last resort — hard rate limit, only for year/label stragglers) ──
def itunes_info(artist: str, title: str) -> dict | None:
    """→ {year, release_date, album, label, art} or None. Sleeps 3.2s per real call."""
    key = f"itunes|{norm(artist)}|{norm(title)}"
    if key in _load_cache():
        return _load_cache()[key]
    t_clean = clean_title_for_query(title)
    term = f"{primary_artist(artist)} {t_clean}"
    has_cjk = bool(re.search(r"[぀-ヿ一-鿿]", title + (artist or "")))
    hits = []
    for country in (("US", "JP") if has_cjk else ("US",)):
        try:
            d = _get_json(f"https://itunes.apple.com/search?media=music&entity=song&limit=6"
                          f"&country={country}&term=" + urllib.parse.quote(term))
        except Exception as e:
            print(f"    itunes error: {e}")
            return None
        for c in d.get("results", []):
            if not _titles_match(title, c.get("trackName", "")):
                continue
            # Cross-script artists (山口美央子 vs Mioko Yamaguchi) can't token-match;
            # accept an EXACT normalized CJK title as sufficient in that case.
            if _artists_match(artist, c.get("artistName", "")) or \
               (has_cjk and norm(title) == norm(c.get("trackName", "")) and len(norm(title)) >= 6):
                hits.append(c)
        time.sleep(3.2)
    # All validated candidates, EARLIEST release first — a song appears on
    # reissues/compilations too, and we want the original release's year/label.
    hits.sort(key=lambda c: c.get("releaseDate") or "9999")
    hit = hits[0] if hits else None
    if not hit:
        return None
    rd = hit.get("releaseDate", "")
    label = None
    if hit.get("collectionId"):
        time.sleep(3.2)
        try:
            look = _get_json(f"https://itunes.apple.com/lookup?id={hit['collectionId']}&entity=album")
            for r in look.get("results", []):
                cp = r.get("copyright") or ""
                m = re.search(r"[℗©]\s*\d{4}\s+(.+?)(?:,|$|\.\s)", cp)
                if m:
                    label = m.group(1).strip()
                    break
        except Exception:
            pass
    art = (hit.get("artworkUrl100") or "").replace("100x100bb", "1000x1000bb") or None
    out = {
        "year": int(rd[:4]) if re.match(r"^\d{4}", rd) else None,
        "release_date": rd[:10] or None,
        "album": hit.get("collectionName"),
        "label": label,
        "art": art,
    }
    _cache_put(key, out)
    time.sleep(3.2)
    return out


# ── authoritative track duration (anti-fake backbone) ───────────────────────
def authoritative_durations(artist: str, title: str) -> list[int]:
    """ALL official lengths (seconds) of validated matches — a song legitimately
    exists in several versions (radio edit / album / extended mix), so anti-fake
    checks must compare against the CLOSEST official version, not just one, or a
    correct 212s radio edit gets flagged against a 364s club mix."""
    key = f"durs|{norm(artist)}|{norm(title)}"
    if key in _load_cache():
        return _load_cache()[key].get("s", [])
    a = primary_artist(artist)
    t_clean = clean_title_for_query(title)
    out: list[int] = []
    t_vals = [t_clean]
    if t_clean != title:
        t_vals.append(re.sub(r"\(feat[^)]*\)", "", title, flags=re.I).strip())
    queries = []
    for tv in t_vals:
        queries.extend([f'track:"{tv}" artist:"{a}"', f"{a} {tv}"])
    for q in queries:
        try:
            d = _get_json("https://api.deezer.com/search?limit=8&q=" + urllib.parse.quote(q))
        except Exception:
            break
        for cand in d.get("data", []):
            if cand.get("duration") and _titles_match(title, cand.get("title", "")) and \
               _artists_match(artist, cand.get("artist", {}).get("name", "")):
                out.append(int(cand["duration"]))
        if out:
            break
        time.sleep(0.15)
    if not out:
        # iTunes fallback — direct validated lookup
        try:
            d = _get_json("https://itunes.apple.com/search?media=music&entity=song&limit=5&term="
                          + urllib.parse.quote(f"{a} {t_clean}"))
            time.sleep(3.2)
            for cand in d.get("results", []):
                if cand.get("trackTimeMillis") and _titles_match(title, cand.get("trackName", "")) and \
                   _artists_match(artist, cand.get("artistName", "")):
                    out.append(round(cand["trackTimeMillis"] / 1000))
        except Exception:
            pass
    out = sorted(set(out))
    if out:
        _cache_put(key, {"s": out})
    return out


def authoritative_duration(artist: str, title: str) -> int | None:
    """Single best official length (first validated hit) — used by the ingest gate."""
    ds = authoritative_durations(artist, title)
    return ds[0] if ds else None


# ── one call that does the whole waterfall ──────────────────────────────────
def enrich(artist: str, title: str, need_itunes_fallback: bool = True) -> dict:
    """Merge all sources by priority. Returns a dict where every value may be None;
    'sources' lists which APIs answered (for logging/decisions)."""
    g = genius_song(artist, title)
    time.sleep(0.15)
    dz = deezer_info(artist, title)
    out = {
        "producers": (g or {}).get("producers"),
        "writers": (g or {}).get("writers"),
        "featuring": (g or {}).get("featuring"),
        "year": (g or {}).get("year"),
        "release_date": (g or {}).get("release_date"),
        "label": (g or {}).get("label"),
        "album": (dz or {}).get("album"),
        "release_type": (dz or {}).get("release_type"),
        "genres": (dz or {}).get("genres"),
        "art_genius": (g or {}).get("art"),
        "art_deezer": (dz or {}).get("art"),
        "sources": [s for s, v in (("genius", g), ("deezer", dz)) if v],
    }
    # Label: Deezer answers fast and is right for modern releases; take it before
    # paying MusicBrainz's 1 req/s. Year: genius → MusicBrainz (original-release
    # authority) → Deezer (reissue risk) → iTunes.
    if out["label"] is None:
        out["label"] = (dz or {}).get("label")
    if out["year"] is None or out["label"] is None:
        mb = musicbrainz_info(artist, title, want_label=out["label"] is None)
        if mb:
            out["year"] = out["year"] or mb.get("year")
            out["release_date"] = out["release_date"] or mb.get("release_date")
            out["label"] = out["label"] or mb.get("label")
            out["album"] = out["album"] or mb.get("album")
            out["sources"].append("musicbrainz")
    if out["year"] is None:
        out["year"] = (dz or {}).get("year")
        out["release_date"] = out["release_date"] or (dz or {}).get("release_date")
    if need_itunes_fallback and (out["year"] is None or out["label"] is None):
        it = itunes_info(artist, title)
        if it:
            out["year"] = out["year"] or it.get("year")
            out["release_date"] = out["release_date"] or it.get("release_date")
            out["label"] = out["label"] or it.get("label")
            out["album"] = out["album"] or it.get("album")
            out["art_itunes"] = it.get("art")
            out["sources"].append("itunes")
    return out


if __name__ == "__main__":
    import sys
    a, t = sys.argv[1], sys.argv[2]
    r = enrich(a, t)
    save_cache()
    print(json.dumps(r, ensure_ascii=False, indent=2))
