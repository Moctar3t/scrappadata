#!/usr/bin/env python3
"""
ingest_playlist.py — verified catalog ingestion from an Exportify CSV.

THE PROBLEM THIS SOLVES
  The old path (`download_single_track.py`) did `ytsearch1:<query>` — the FIRST
  YouTube hit, no checks — so covers / lives / "(sped up)" / lyric-video-with-
  wrong-audio constantly won. And metadata came from a `limit=1` iTunes fuzzy
  search → wrong-release cover art. Nothing ever verified "is this the real song".

THE GATE (why a cover can't get through)
  Identity is the Exportify CSV row (Spotify's own canonical data: exact title /
  artist / album / duration / BPM / key / popularity). For each track we pull N
  YouTube candidates (metadata only — no download, no ffmpeg needed) and SCORE them:

    • `album` field populated  → it's a YouTube "Art Track" = the label-distributed
      master audio (the strongest single signal). We then cross-check its exposed
      album/artist against the CSV — a keyless metadata fingerprint.
    • duration within ±2 s of the CSV duration → it's THE recording, not a live /
      extended / sped-up edit. >8 s off  → hard reject.
    • title contains live/cover/remix/sped/slowed/8d/lyric/karaoke/instrumental/
      mashup/nightcore/edit  → hard reject.
    • channel == artist, exact title match, "official audio" → smaller bonuses.

  A track only becomes `accepted` if a candidate clears the bar; otherwise it's
  `manual` (plausible but unproven) or `rejected` (nothing fits) — NEVER silently
  wrong. AcoustID/Chromaprint fingerprinting is a clean drop-in on top (see
  verify_fingerprint) once a free key is added; the album+duration gate already
  nails catalogue artists without it.

OUTPUT
  ingest_manifest.json — per track: canonical metadata + chosen YouTube url + score
                         + status + reasons. Feeds the existing download→demucs→
                         smart_link steps (the chosen url replaces the blind search).
  ingest_report.md     — human-readable accepted / manual / rejected with reasons.

USAGE
  python3 ingest_playlist.py /path/to/exportify.csv [--limit N] [--candidates 8]
                                                    [--out-dir DIR]
"""
from __future__ import annotations
import argparse, csv, json, os, re, sys, time, urllib.parse, urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    sys.exit("yt-dlp not installed.  python3 -m pip install --user yt-dlp")

# ── tuning knobs (the whole gate in one place) ──────────────────────────────
DURATION_TIGHT_S = 2      # within this of the CSV duration → it's the recording
DURATION_OK_S    = 5      # still plausible
DURATION_VIDEO_S = 30     # an official music VIDEO may run this much longer (intro/outro)
                          # → still usable when no clean audio master exists
MANUAL_THRESHOLD = 20     # nothing reaches a tier but a candidate exists → flag, don't drop

REJECT_WORDS = [
    "live", "cover", "remix", "sped up", "sped-up", "spedup", "slowed", "reverb",
    "8d audio", "lyric", "karaoke", "instrumental", "mashup", "nightcore",
    "remaster live", "concert", "tribute", "acoustic cover", "reaction",
    "bass boosted", "loop", "1 hour", "extended mix", "edit)",
]

# Spotify pitch-class (0..11) → name; mode 1=Major 0=Minor.  Matches the catalogue
# format the librosa scraper used ("G# Major") so Mixima/Mervec keyScore parses it.
PITCH = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


@dataclass
class Candidate:
    vid: str
    title: str
    channel: str
    duration: int
    album: str | None
    artist: str | None
    score: int = 0
    kind: str = "other"      # master_art | master_audio | video | other
    reasons: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.vid}"


@dataclass
class Track:
    spotify_id: str
    title: str
    artists: list[str]
    album: str
    duration_ms: int
    release_date: str = ""
    popularity: int = 0
    genres: str = ""
    label: str = ""
    bpm: float | None = None
    musical_key: str | None = None
    features: dict = field(default_factory=dict)
    art_url: str | None = None            # PRIMARY cover (Genius art when found)
    art_url_fallback: str | None = None   # the Spotify/oEmbed art, kept as fallback
    # enriched from Genius (producers/featuring/writers/year + hi-res art)
    producers: list = field(default_factory=list)
    featuring: list = field(default_factory=list)
    writers: list = field(default_factory=list)
    year: int | None = None
    # filled by the gate
    status: str = "pending"          # accepted | manual | rejected
    source_kind: str = ""            # master_art | master_audio | video
    youtube_url: str | None = None
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000.0


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def parse_csv(path: Path, limit: int | None) -> list[Track]:
    """Read an Exportify CSV by HEADER NAME (robust to column order / missing
    cols across Exportify versions — this export has no ISRC/art but has audio
    features)."""
    out: list[Track] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        # case/space-insensitive header lookup
        keymap = {re.sub(r"[^a-z0-9]+", "", k.lower()): k for k in reader.fieldnames or []}

        def col(row, *names, default=""):
            for n in names:
                k = keymap.get(re.sub(r"[^a-z0-9]+", "", n.lower()))
                if k and row.get(k) not in (None, ""):
                    return row[k]
            return default

        for row in reader:
            title = col(row, "Track Name").strip()
            artists_raw = col(row, "Artist Name(s)", "Artist Name").strip()
            if not title or not artists_raw:
                continue
            uri = col(row, "Track URI")
            sid = uri.split(":")[-1] if uri else ""
            dur_ms = int(col(row, "Duration (ms)", "Track Duration (ms)", default="0") or 0)

            key = col(row, "Key", default="")
            mode = col(row, "Mode", default="")
            musical_key = None
            if key != "" and mode != "":
                try:
                    musical_key = f"{PITCH[int(float(key))]} {'Major' if int(float(mode)) == 1 else 'Minor'}"
                except (ValueError, IndexError):
                    pass
            tempo = col(row, "Tempo", default="")
            bpm = round(float(tempo)) if tempo not in ("", None) else None

            feats = {}
            for fn in ["Danceability", "Energy", "Loudness", "Speechiness",
                       "Acousticness", "Instrumentalness", "Liveness", "Valence",
                       "Time Signature"]:
                v = col(row, fn, default="")
                if v != "":
                    try: feats[_norm(fn).replace(" ", "_")] = float(v)
                    except ValueError: pass

            rd = col(row, "Release Date", "Album Release Date")
            out.append(Track(
                spotify_id=sid, title=title,
                artists=[a.strip() for a in artists_raw.split(";") if a.strip()],
                album=col(row, "Album Name").strip(),
                duration_ms=dur_ms,
                release_date=rd,
                year=int(rd[:4]) if rd[:4].isdigit() else None,
                popularity=int(col(row, "Popularity", default="0") or 0),
                genres=col(row, "Genres"), label=col(row, "Record Label"),
                bpm=bpm, musical_key=musical_key, features=feats,
            ))
            if limit and len(out) >= limit:
                break
    return out


def _parse_title_artist(video_title: str, channel: str) -> tuple[str, list[str]]:
    """Best-effort identity from a non-Art-Track video title like
    'Artist - Title (Official Video)'. Falls back to the channel as artist."""
    t = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*", " ", video_title).strip()  # drop (...)/[...]
    if " - " in t:
        a, _, ti = t.partition(" - ")
        return ti.strip(), [a.strip()]
    # no separator → title is the whole thing, artist from channel (minus "- Topic"/"VEVO")
    ch = re.sub(r"\s*-\s*topic$|vevo$", "", channel.strip(), flags=re.I).strip()
    return t, ([ch] if ch else [])


def parse_youtube(source: str, limit: int | None, ydl) -> list[Track]:
    """Second entry point: a YouTube playlist (or one/more video URLs). Here each
    VIDEO is already the chosen source, so the gate's job shifts from 'find the right
    upload' to 'extract clean identity + reject obvious covers/lives'. Art-Track
    videos hand us album/artist/track directly; regular videos get title-parsed.
    BPM/key aren't on YouTube → left null and flagged for enrichment (the CSV path
    gets them from Spotify)."""
    try:
        info = ydl.extract_info(source, download=False)
    except Exception as e:
        info = None
        print(f"  [youtube] extraction failed: {e}")

    if not info:
        print("  [youtube] standard request blocked. Retrying with Chrome cookies...")
        try:
            ydl_opts = {**ydl.params, "cookiesfrombrowser": ["chrome"]}
            ydl = yt_dlp.YoutubeDL(ydl_opts)
            info = ydl.extract_info(source, download=False)
        except Exception as e:
            print(f"  [youtube] retry with Chrome cookies failed: {e}")
            info = None

    if not info:
        print("  [youtube] ERROR: Youtube metadata extraction failed completely. YouTube is likely blocking this client.")
        return []

    entries = info.get("entries") if info.get("_type") == "playlist" or "entries" in info else [info]
    out: list[Track] = []
    for e in (entries or []):
        if not e:
            continue
        # flat-playlist entries lack duration/album — re-extract that one fully
        if e.get("duration") is None and e.get("id"):
            try:
                e = ydl.extract_info(f"https://www.youtube.com/watch?v={e['id']}", download=False)
            except Exception:
                pass
        vtitle = e.get("title", "")
        album = e.get("album")
        artist = e.get("artist") or e.get("creator")
        track_title = e.get("track")
        if track_title and artist:                       # Art Track — clean identity
            title, artists = track_title, [a.strip() for a in re.split(r"[;,&]| feat", artist) if a.strip()]
        else:
            title, artists = _parse_title_artist(vtitle, e.get("channel") or e.get("uploader") or "")
        t = Track(
            spotify_id="", title=title, artists=artists or ["Unknown"],
            album=album or "", duration_ms=int((e.get("duration") or 0) * 1000),
        )
        t.youtube_url = f"https://www.youtube.com/watch?v={e.get('id','')}"
        tl = vtitle.lower()
        if any(w in tl for w in REJECT_WORDS):
            t.status = "rejected"; t.reasons.append(f"reject-word in '{vtitle[:40]}'")
        elif t.duration_ms > 900_000:
            t.status = "rejected"; t.reasons.append(f"duration exceeds 15 minutes limit ({t.duration_s:.0f}s)")
        elif not title or not artists:
            t.status = "manual"; t.reasons.append("weak identity — parse failed")
        else:
            # ANTI-FAKE for playlist drops: a CSV carries the official duration, a
            # YouTube video only carries ITS OWN — self-confirming if the video is a
            # cover/sped-up/bootleg. Cross-check against the official recording
            # length (Deezer/iTunes, validated match). >8s off → not the recording.
            from meta_sources import authoritative_duration
            auth = None
            try:
                auth = authoritative_duration("; ".join(t.artists), t.title)
            except Exception:
                pass
            if auth and t.duration_s and abs(t.duration_s - auth) > 8:
                t.status = "manual"
                t.reasons.append(f"NOT the official recording? video {t.duration_s:.0f}s "
                                 f"vs official {auth}s — review before ingesting")
            else:
                t.status = "accepted"
                t.source_kind = "master_art" if album else "video"
                t.reasons.append("art-track" if album else "video (title-parsed)")
                if auth:
                    t.reasons.append(f"duration verified vs official ({auth}s)")
                t.reasons.append("⚠ no BPM/key — needs enrichment")
        out.append(t)
        if limit and len(out) >= limit:
            break
    return out


GENIUS_CACHE = Path(__file__).resolve().parent / "genius_enrich_cache.json"
_GENIUS_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _genius_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _GENIUS_UA})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def enrich_genius(track: Track, cache: dict) -> None:
    """Fill producers / featuring / writers / year + hi-res cover art via
    meta_sources.genius_song — the OFFICIAL api.genius.com with our token (the
    unofficial genius.com/api endpoint IP-bans and silently returns nothing).
    Matches are already title+artist validated inside meta_sources, so a wrong
    Genius hit can't graft another song's credits on. BPM/key are NOT taken from
    Genius — those stay authoritative from Spotify."""
    from meta_sources import genius_song
    try:
        g = genius_song("; ".join(track.artists), track.title)
    except Exception as e:
        track.reasons.append(f"genius error: {str(e)[:60]}")
        return
    if not g:
        track.reasons.append("genius: no match")
        return
    track.producers = [p.strip() for p in (g.get("producers") or "").split(";") if p.strip()]
    track.featuring = [p.strip() for p in (g.get("featuring") or "").split(",") if p.strip()]
    track.writers = [p.strip() for p in (g.get("writers") or "").split(";") if p.strip()]
    if not track.year and g.get("year"):
        track.year = g["year"]
    if g.get("art"):                      # Genius song art is hi-res — prefer it
        track.art_url = g["art"]


def fetch_art(track: Track) -> str | None:
    """Authoritative album art via Spotify's keyless oEmbed endpoint."""
    if not track.spotify_id:
        return None
    try:
        u = "https://open.spotify.com/oembed?url=" + urllib.parse.quote(
            f"https://open.spotify.com/track/{track.spotify_id}", safe="")
        req = urllib.request.Request(u, headers={"User-Agent": "zingy-ingest/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("thumbnail_url")
    except Exception:
        return None


def search_candidates(track: Track, n: int, ytm) -> list[Candidate]:
    query = f"{track.primary_artist} {track.title}"
    try:
        results = ytm.search(query, filter="songs", limit=n)
    except Exception as e:
        track.reasons.append(f"search failed: {e}")
        return []

    cands = []
    for e in results:
        # We only care about songs and videos
        if e.get("resultType") not in ("song", "video"):
            continue
            
        vid = e.get("videoId")
        if not vid:
            continue
            
        album = (e.get("album") or {}).get("name") if e.get("resultType") == "song" else None
        artist_names = [a.get("name") for a in e.get("artists", []) if a.get("name")]
        
        cands.append(Candidate(
            vid=vid,
            title=e.get("title", ""),
            channel=artist_names[0] if artist_names else "",
            duration=e.get("duration_seconds") or 0,
            album=album,
            artist=", ".join(artist_names),
        ))
    return cands


def score(track: Track, c: Candidate) -> Candidate:
    """Score a candidate AND classify its `kind` into a tier:
       master_art   — YouTube Art Track (album metadata = label master audio)
       master_audio — clean audio, tight duration, clearly this artist/title
       video        — official music video (right artist/title) but no audio master;
                      usable when nothing better exists, even if it runs a bit long
                      (intro/outro) — "if a song only has a music video, use it".
    A reject-word (live/cover/remix/…) drops it out of every tier."""
    r = c.reasons
    tl = c.title.lower()
    for w in REJECT_WORDS:
        if w in tl:
            c.score = -1000; c.kind = "reject"; r.append(f"reject-word '{w}'"); return c

    dd = 0 if track.duration_s == 0 else (abs(c.duration - track.duration_s) if c.duration else 999)
    # OFFICIAL-SOURCE check — the candidate's artist/channel must BE this artist.
    # Cross-script names (竹内まりや ⇔ Mariya Takeuchi) resolve via MusicBrainz
    # aliases so the check has teeth on non-Latin catalogues instead of failing open.
    names = set(track.artists)
    try:
        from meta_sources import artist_aliases
        names |= artist_aliases(track.artists[0] if track.artists else "")
    except Exception:
        pass
    artist_match = any(_norm(a) and (_norm(a) in _norm(c.channel) or
                                     (c.artist and (_norm(a) in _norm(c.artist) or
                                                    _norm(c.artist) in _norm(a))))
                       for a in names if a)
    title_match = _norm(track.title) in _norm(c.title)
    is_video = "official video" in tl or "music video" in tl or "(video" in tl

    # duration scoring + reasons
    if c.duration == 0:
        r.append("no duration")
    elif dd <= DURATION_TIGHT_S:
        c.score += 45; r.append(f"dur±{dd:.0f}s")
    elif dd <= DURATION_OK_S:
        c.score += 25; r.append(f"dur±{dd:.0f}s")
    elif dd <= DURATION_VIDEO_S:
        c.score += 5; r.append(f"dur+{dd:.0f}s (video pad)")
    else:
        c.score = -1000; c.kind = "reject"; r.append(f"dur±{dd:.0f}s too far"); return c

    if artist_match:
        c.score += 16; r.append("artist-match")
    if title_match:
        c.score += 12; r.append("title-match")

    # HARD RULE: no tier without the artist. Cover bands and karaoke labels publish
    # Art Tracks too, and a cover titled "Song (Artist)" from a random channel can
    # nail both title and duration — the ONLY signal that separates the real thing
    # is that the audio comes from the artist's own catalog entry or channel.
    if not artist_match:
        c.kind = "unverified" if (title_match and dd <= DURATION_OK_S) else "other"
        if c.kind == "unverified":
            r.append("right title/length but NOT this artist's channel/catalog")
        return c

    if c.album:                                    # Art Track = master audio
        c.score += 45; r.append(f"art-track({c.album})")
        if track.album and _norm(c.album) == _norm(track.album):
            c.score += 20; r.append("album==csv")
        c.kind = "master_art" if dd <= DURATION_OK_S else "video"
    elif dd <= DURATION_TIGHT_S:
        c.score += 8; r.append("audio-master")
        c.kind = "master_audio"
    elif dd <= DURATION_VIDEO_S and (title_match or "official" in tl):
        c.kind = "video"
        c.score += (8 if is_video else 4); r.append("official-video" if is_video else "official-source")
    else:
        c.kind = "other"
    return c


TIER_ORDER = {"master_art": 3, "master_audio": 2, "video": 1, "unverified": 0.5,
              "other": 0, "reject": -1}


def pick(track: Track, cands: list[Candidate]) -> None:
    """Tiered selection: take the best AUDIO MASTER if one exists; else fall back to
    the best official music VIDEO; never drop a song just because it's video-only.
    Anything with no usable tier but a live candidate → manual (human eyes), not lost."""
    if not cands:
        track.status = "rejected"; track.reasons.append("no candidates"); return
    for c in cands:
        score(track, c)
    # rank by tier first, then score within tier
    cands.sort(key=lambda c: (TIER_ORDER.get(c.kind, 0), c.score), reverse=True)
    best = cands[0]
    track.score = best.score
    track.reasons = best.reasons + track.reasons

    if best.kind in ("master_art", "master_audio", "video"):
        track.status = "accepted"
        track.source_kind = best.kind
        track.youtube_url = best.url
        if best.kind == "video":
            track.reasons.append("video fallback — no clean audio master found")
    elif best.kind == "unverified":
        # right title + right length but NOT from the artist's channel/catalog —
        # this is the exact fingerprint of a good cover. Human eyes, never auto.
        track.status = "manual"; track.youtube_url = best.url
        track.reasons.append("OFFICIAL SOURCE NOT FOUND — best candidate matches title/"
                             "length but isn't from the artist (possible cover)")
    elif best.score >= MANUAL_THRESHOLD:
        track.status = "manual"; track.youtube_url = best.url
        track.reasons.append(f"no master/video tier (score {best.score}) — needs eyes")
    else:
        track.status = "rejected"
        track.reasons.append(f"nothing usable (best {best.score})")


def verify_fingerprint(track: Track, audio_path: Path) -> bool:
    """Hook for AcoustID/Chromaprint hard verification once a free key + fpcalc are
    available (the belt-and-suspenders layer). Returns True until wired so the
    album+duration gate stands alone."""
    return True


def enqueue_to_vps(tracks: list[Track], api: str, secret: str) -> None:
    """POST accepted tracks to the VPS catalog queue (POST /internal/split/catalog,
    worker-secret guarded). The home GPU worker then claims them (pull model),
    yt-dlp's the YouTube source, separates, and reports stem URLs back. Idempotent
    server-side on (catalog owner, youtubeUrl), so re-running is safe."""
    url = api.rstrip("/") + "/internal/split/catalog"
    sent = skipped = 0
    for t in tracks:
        if t.status != "accepted" or not t.youtube_url:
            continue
        payload = {
            "youtubeUrl": t.youtube_url,
            "title": t.title,
            "artist": "; ".join(t.artists) if t.artists else None,
            "bpm": t.bpm,
            "songKey": t.musical_key,
            "durationSec": round(t.duration_ms / 1000) if t.duration_ms else None,
            "coverUrl": t.art_url,
            "coverUrlFallback": t.art_url_fallback,
            "year": t.year,
            "producers": "; ".join(t.producers) if t.producers else None,
            "featuring": "; ".join(t.featuring) if t.featuring else None,
            "writers": "; ".join(t.writers) if t.writers else None,
            "releaseDate": t.release_date or None,
            "recordLabel": t.label or None,
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "x-worker-secret": secret})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.load(r)
            tag = " (dedup)" if res.get("dedup") else ""
            print(f"  → {t.primary_artist} — {t.title}: {res.get('id', '?')}{tag}")
            sent += 1
        except Exception as e:
            print(f"  ✗ {t.primary_artist} — {t.title}: {e}")
            skipped += 1
    print(f"enqueued {sent} accepted track(s){f', {skipped} failed' if skipped else ''} → {url}")


def sanitize_youtube_url(url: str) -> str:
    url = url.strip()
    if not (url.startswith(("http://", "https://", "www.")) or "youtube.com" in url or "youtu.be" in url):
        return url
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)

    vid = None
    if "v" in qs:
        vid = qs["v"][0]
    elif parsed.netloc == "youtu.be" or parsed.netloc.endswith(".youtu.be"):
        vid = parsed.path.strip("/")

    playlist_id = None
    if "list" in qs:
        playlist_id = qs["list"][0]

    is_mix_or_radio = False
    if playlist_id and playlist_id.startswith("RD"):
        is_mix_or_radio = True
    if "start_radio" in qs:
        is_mix_or_radio = True

    if is_mix_or_radio:
        if vid:
            print(f"[sanitize] mix/radio link detected. Resolving to seed video: https://www.youtube.com/watch?v={vid}")
            return f"https://www.youtube.com/watch?v={vid}"
        elif playlist_id:
            return f"https://www.youtube.com/playlist?list={playlist_id}"

    if playlist_id:
        return f"https://www.youtube.com/playlist?list={playlist_id}"
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    return url


def main():
    ap = argparse.ArgumentParser(description="Verified catalog ingestion (Exportify CSV or YouTube playlist).")
    ap.add_argument("source", help="Exportify CSV path  OR  a YouTube playlist/video URL")
    ap.add_argument("--limit", type=int, default=None, help="process only the first N tracks")
    ap.add_argument("--candidates", type=int, default=8, help="YouTube candidates per CSV track to score")
    ap.add_argument("--out-dir", type=Path, default=Path.cwd())
    ap.add_argument("--no-art", action="store_true", help="skip Spotify art lookup (faster)")
    ap.add_argument("--no-genius", action="store_true",
                    help="skip Genius enrichment (producers/featuring/writers/year/art)")
    ap.add_argument("--enqueue", action="store_true",
                    help="POST accepted tracks to the catalog queue (needs ZINGY_WORKER_SECRET)")
    ap.add_argument("--api", default="https://zingy.dime.systems/api", help="VPS API base")
    args = ap.parse_args()
    args.source = sanitize_youtube_url(args.source)

    is_url = args.source.startswith(("http://", "https://", "www.")) or "youtube.com" in args.source or "youtu.be" in args.source
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                            "extract_flat": False, "ignoreerrors": True})

    # Load existing app catalogue to prevent Title+Artist duplicates
    existing_catalog = set()
    def norm_string(s):
        import re
        return re.sub(r'[^a-z0-9]', '', str(s).lower()) if s else ""

    try:
        catalog_path = Path("/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata/music-index.json")
        if catalog_path.exists():
            catalog_data = json.loads(catalog_path.read_text(encoding="utf-8"))
            songs_list = catalog_data.get("songsList", []) if isinstance(catalog_data, dict) else catalog_data
            for song in songs_list:
                existing_catalog.add( (norm_string(song.get('artist', '')), norm_string(song.get('title', ''))) )
            print(f"Loaded {len(existing_catalog)} existing songs from app catalogue for deduplication.\n")
    except Exception as e:
        print(f"Warning: Could not load existing app catalog for deduplication: {e}")


    if is_url:                                            # ── YouTube-playlist entry point ──
        print(f"Source: YouTube — {args.source}\n")
        tracks = parse_youtube(args.source, args.limit, ydl)
        print(f"Extracted {len(tracks)} tracks\n")
    else:                                                 # ── Exportify CSV entry point ──
        csv_path = Path(args.source)
        if not csv_path.exists():
            sys.exit(f"Not a URL and CSV not found: {csv_path}")
        tracks = parse_csv(csv_path, args.limit)
        print(f"Source: CSV — {csv_path.name}, {len(tracks)} tracks\n")
        YOUTUBE_CACHE = Path(__file__).resolve().parent / "youtube_search_cache.json"
        yt_cache = {}
        if YOUTUBE_CACHE.exists():
            try:
                yt_cache = json.loads(YOUTUBE_CACHE.read_text(encoding="utf-8"))
            except Exception:
                pass

        try:
            from ytmusicapi import YTMusic
            ytm = YTMusic()
        except ImportError:
            sys.exit("ytmusicapi not installed. Run: python3 -m pip install ytmusicapi")



        print(f"Phase 1: Searching YouTube for {len(tracks)} tracks...")
        for i, t in enumerate(tracks, 1):
            query = f"{t.primary_artist} {t.title}"

            t_str = norm_string(t.title)
            a_str = norm_string(t.primary_artist)
            
            is_dup = False
            # Check for strict exact matches to prevent false positives (e.g. 'Me' inside 'Me and You')
            if existing_catalog and t_str and a_str:
                for (cat_a, cat_t) in existing_catalog:
                    if t_str == cat_t or (t_str in cat_t and a_str in cat_a): 
                        # We use t_str == cat_t for exact title matches.
                        # If the app has "Title (feat. X)", we also accept it if the core title is a substring AND the artist matches
                        if t_str == cat_t and (a_str == cat_a or a_str in cat_a or cat_a in a_str):
                            is_dup = True
                            break
                        elif t_str in cat_t and (a_str == cat_a or a_str in cat_a):
                            is_dup = True
                            break

            if is_dup:
                t.status = "rejected"
                t.reasons.append("already in zingy app")
                print(f"[Search: {i:>3}/{len(tracks)}] ✗ rejected   {t.primary_artist} — {t.title} (already in app)")
                continue

            if query in yt_cache:
                cands = [Candidate(**c) for c in yt_cache[query]]
                pick(t, cands)
                continue

            cands = search_candidates(t, args.candidates, ytm)
            pick(t, cands)
            yt_cache[query] = [asdict(c) for c in cands]
            
            # Print live progress so the user isn't blind during the long wait
            mark = {"accepted": "✓", "manual": "?", "rejected": "✗"}[t.status]
            print(f"[Search: {i:>3}/{len(tracks)}] {mark} {t.status:<8} {t.primary_artist} — {t.title}")
            
            # Save cache incrementally
            if i % 10 == 0:
                YOUTUBE_CACHE.write_text(json.dumps(yt_cache, indent=2, ensure_ascii=False))

        # Final save
        YOUTUBE_CACHE.write_text(json.dumps(yt_cache, indent=2, ensure_ascii=False))
        print("Phase 1 Complete.\n")

    cache = {}
    if GENIUS_CACHE.exists():
        try:
            cache = json.loads(GENIUS_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass

    for i, t in enumerate(tracks, 1):
        if not args.no_genius and t.status != "rejected":
            enrich_genius(t, cache)

        # DEDUPLICATION PASS 2 (Post-Enrichment)
        # For YouTube playlists, t.title and t.primary_artist were dirty. Genius just cleaned them.
        # Now we check against the existing_catalog.
        if t.status != "rejected" and existing_catalog:
            t_str = norm_string(t.title)
            a_str = norm_string(t.primary_artist)
            if t_str and a_str:
                for (cat_a, cat_t) in existing_catalog:
                    if t_str == cat_t or (t_str in cat_t and a_str in cat_a): 
                        if t_str == cat_t and (a_str == cat_a or a_str in cat_a or cat_a in a_str):
                            t.status = "rejected"
                            t.reasons.append("already in zingy app")
                            break
                        elif t_str in cat_t and (a_str == cat_a or a_str in cat_a):
                            t.status = "rejected"
                            t.reasons.append("already in zingy app")
                            break

        if not args.no_art and t.status != "rejected" and t.spotify_id:
            # Genius art stays PRIMARY; Spotify's oEmbed art rides as the fallback
            # (never overwrite — that silently threw the Genius art away before).
            sp = fetch_art(t)
            if sp:
                if t.art_url:
                    t.art_url_fallback = sp
                else:
                    t.art_url = sp
        mark = {"accepted": "✓", "manual": "?", "rejected": "✗"}[t.status]
        src = f"[{t.source_kind}]" if t.source_kind else ""
        print(f"[{i:>3}/{len(tracks)}] {mark} {t.status:<8} {src:<14} {t.primary_artist} — {t.title}"
              f"  (s{t.score})  {', '.join(t.reasons[:3])}")
              
    if cache and not args.no_genius:
        GENIUS_CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))

    # ── outputs ─────────────────────────────────────────────────────────────
    by = {"accepted": [], "manual": [], "rejected": []}
    for t in tracks:
        by[t.status].append(t)

    manifest = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(args.source),
        "counts": {k: len(v) for k, v in by.items()},
        "tracks": [asdict(t) for t in tracks],
    }
    (args.out_dir / "ingest_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    lines = [f"# Ingest report — {manifest['generated']}", "",
             f"Source: `{args.source}`  ·  "
             f"**{len(by['accepted'])} accepted · {len(by['manual'])} manual · {len(by['rejected'])} rejected**", ""]
    for status, head in [("manual", "⚠️ Needs review"), ("rejected", "✗ Rejected")]:
        if by[status]:
            lines.append(f"## {head} ({len(by[status])})\n")
            for t in by[status]:
                lines.append(f"- **{t.primary_artist} — {t.title}** (score {t.score}) — "
                             f"{', '.join(t.reasons[:4])}"
                             + (f"  \n  {t.youtube_url}" if t.youtube_url else ""))
            lines.append("")
    (args.out_dir / "ingest_report.md").write_text("\n".join(lines))

    print(f"\n{'='*60}\n✓ {len(by['accepted'])}   ? {len(by['manual'])}   ✗ {len(by['rejected'])}")
    print(f"manifest → {args.out_dir/'ingest_manifest.json'}")
    print(f"report   → {args.out_dir/'ingest_report.md'}")

    if args.enqueue:
        secret = os.environ.get("ZINGY_WORKER_SECRET")
        if not secret:
            print("\n⚠ --enqueue needs ZINGY_WORKER_SECRET env var — skipped.")
        else:
            print(f"\nEnqueuing {len(by['accepted'])} accepted track(s) to the catalog queue…")
            enqueue_to_vps(tracks, args.api, secret)


if __name__ == "__main__":
    main()
