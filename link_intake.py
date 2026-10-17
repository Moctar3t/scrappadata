#!/usr/bin/env python3
"""
link_intake.py — a pasted link becomes rows the ingest already understands.

  Spotify  track · album · playlist     open.spotify.com/…, spotify:track:…
  YouTube  video · playlist             youtube.com/watch, youtu.be, music.youtube.com, shorts

The output is the Exportify CSV shape (build_add_csv.COLUMNS) plus a few
optional columns ingest_gpu.parse_csv reads: a PINNED source (the video you
sent), a cover to fall back on, and a note saying how the song was identified.
From there it is the same ingest every CSV goes through.

WHY NOT SPOTIFY'S WEB API
  Since 2026 it answers this app's every call with "Active premium subscription
  required for the owner of the app". The public embed page
  (open.spotify.com/embed/<kind>/<id>) carries what identity needs — title,
  every artist, runtime, release date, explicit, cover — with no key at all.
  Deezer (keyless) fills album, label and genres.

A YOUTUBE LINK NAMES A VIDEO, NOT A RECORDING. Three outcomes:
  1. Deezer knows the song and the video runs within ±5 s of it → Deezer's
     identity, and the video you sent IS the source (pinned: no search).
  2. Deezer knows the song but the video is another cut (a music-video intro,
     a skit) → Deezer's identity, no pin: resolve finds the master exactly as
     it does for a CSV row, so the catalogue gets the release, not the clip.
  3. Nobody knows it (a leak, a freestyle) → the video's own identity (YouTube
     Music's track/artist fields when it has them, else "Artist - Title" from
     its title), its own runtime, pinned. BPM and key are measured from the
     audio; Genius is still asked for credits and cover.
  An age-gated video can't be read without signing in: its title (oEmbed) is
  used to identify it and resolve looks for an open upload of the same song.
  Among the matching recordings, the EARLIEST release wins: Deezer lists the
  same master on every compilation that reissued it.

YouTube is read on the VPS (vps_line.py info, through the Mac's tunnel like the
rest of the replicate line): its yt-dlp is current, while the Mac's only Python
(3.9) is capped at a yt-dlp YouTube now answers with "page needs to be reloaded".

Nothing is invented: a field no source gave stays empty.

  python3 link_intake.py "<link>"            print what the link becomes (dry run)
"""
from __future__ import annotations
import concurrent.futures as cf
import json, re, sys, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ingest_gpu import title_matches, atoks, ntitle, DURATION_TOLERANCE_S  # noqa: E402  one gate
import build_add_csv as bac                                                    # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
EXTRA = ["Source URL", "Source Title", "Source Channel", "Source Duration (s)",
         "Cover URL", "Cover Source", "Intake Note"]
COLUMNS = bac.COLUMNS + EXTRA
MAX_TRACKS = 100          # one link, at most this many songs (each one is a paid Replicate split)

class IntakeError(RuntimeError):
    pass

def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def _json(url: str) -> dict:
    return json.loads(_get(url))

# ── what kind of link ───────────────────────────────────────────────────────
def classify(url: str) -> tuple[str, str, str]:
    """→ (provider, kind, id). Raises IntakeError for anything else."""
    u = (url or "").strip()
    m = re.match(r"spotify:(track|album|playlist):([A-Za-z0-9]{22})$", u)
    if m:
        return "spotify", m.group(1), m.group(2)
    p = urllib.parse.urlparse(u if "://" in u else "https://" + u)
    host = (p.hostname or "").lower()
    if host.endswith("spotify.com"):
        m = re.search(r"/(track|album|playlist)/([A-Za-z0-9]{22})", p.path)
        if m:
            return "spotify", m.group(1), m.group(2)
        raise IntakeError("lien Spotify non pris en charge (piste, album ou playlist seulement)")
    if host in ("youtu.be",):
        vid = p.path.strip("/").split("/")[0]
        if re.fullmatch(r"[\w-]{11}", vid):
            return "youtube", "video", vid
    if host.endswith("youtube.com"):
        q = urllib.parse.parse_qs(p.query)
        m = re.match(r"/(shorts|live|embed)/([\w-]{11})", p.path)
        if m:
            return "youtube", "video", m.group(2)
        if q.get("v") and re.fullmatch(r"[\w-]{11}", q["v"][0]):
            return "youtube", "video", q["v"][0]          # a video, even if it sits in a playlist
        if q.get("list"):
            return "youtube", "playlist", q["list"][0]
    raise IntakeError("ce n'est pas un lien Spotify ou YouTube reconnu")

# ── Spotify, through the public embed page ──────────────────────────────────
def spotify_entity(kind: str, sid: str) -> dict:
    html = _get(f"https://open.spotify.com/embed/{kind}/{sid}").decode("utf-8", "replace")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise IntakeError(f"Spotify n'a rien renvoyé pour ce {kind} (lien privé ou supprimé ?)")
    try:
        return json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError, json.JSONDecodeError):
        raise IntakeError("la page Spotify a changé de forme — link_intake.spotify_entity à revoir")

def _best_image(ent: dict) -> str | None:
    imgs = (ent.get("visualIdentity") or {}).get("image") or []
    imgs = sorted(imgs, key=lambda i: i.get("maxWidth") or 0)
    return imgs[-1].get("url") if imgs else None

def _release_date(ent: dict) -> str:
    rd = ent.get("releaseDate") or {}
    iso = rd.get("isoString") if isinstance(rd, dict) else rd
    return (iso or "")[:10]

def spotify_track_row(ent: dict, album: str = "") -> dict:
    artists = [a.get("name") for a in ent.get("artists") or [] if a.get("name")]
    r = blank()
    r.update({
        "Track URI": ent.get("uri") or f"spotify:track:{ent.get('id')}",
        "Track Name": ent.get("name") or ent.get("title") or "",
        "Artist Name(s)": ";".join(artists),
        "Album Name": album,
        "Release Date": _release_date(ent),
        "Duration (ms)": ent.get("duration") or "",
        "Explicit": str(bool(ent.get("isExplicit"))).lower(),
        "Cover URL": _best_image(ent) or "",
        "Cover Source": "spotify",
        "Intake Note": "identifié par Spotify",
    })
    return r

def spotify_rows(kind: str, sid: str) -> tuple[list[dict], list[str]]:
    ent = spotify_entity(kind, sid)
    if kind == "track":
        return [deezer_fill(spotify_track_row(ent))], []
    notes = []
    uris = [t["uri"] for t in ent.get("trackList") or [] if (t.get("uri") or "").startswith("spotify:track:")]
    if not uris:
        raise IntakeError(f"aucune piste dans ce {kind}")
    if len(uris) >= 100 and kind == "playlist":
        notes.append("Spotify ne montre que les 100 premières pistes d'une playlist")
    if len(uris) > MAX_TRACKS:
        raise IntakeError(f"{len(uris)} pistes : au-delà de la limite de {MAX_TRACKS} par lien")
    album = ent.get("name", "") if kind == "album" else ""
    # The list carries artists only as one comma-joined line ("Tyler, The Creator"
    # can't be split back), so each track's own page gives the real artist list.
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        ents = list(pool.map(lambda u: spotify_entity("track", u.split(":")[-1]), uris))
    return [deezer_fill(spotify_track_row(e, album)) for e in ents], notes

# ── Deezer: album, label, genres; and identity for YouTube links ───────────
def deezer_fill(r: dict) -> dict:
    """Album / label / genres / a missing release date, from Deezer. Fills gaps only."""
    import meta_sources
    try:
        d = meta_sources.deezer_info(r["Artist Name(s)"].replace(";", ", "), r["Track Name"])
    except Exception:
        d = None
    if d:
        r["Album Name"] = r["Album Name"] or (d.get("album") or "")
        r["Record Label"] = r["Record Label"] or (d.get("label") or "")
        r["Release Date"] = r["Release Date"] or (d.get("release_date") or "")
        r["Genres"] = r["Genres"] or ", ".join(d.get("genres") or [])
        if not r["Cover URL"] and d.get("art"):
            r["Cover URL"], r["Cover Source"] = d["art"], "deezer"
    return r

# A word that makes a recording a DIFFERENT recording of the same song. Deezer's
# search ranks "Never Gonna Give You Up (Pianoforte)" next to the original, and
# a piano cover can share the runtime to the second — so a candidate carrying one
# of these words is only accepted when the title we want carries it too.
VERSION = re.compile(r"\b(live|remix|rmx|cover|karaoke|instrumental|acoustic|piano(?:forte)?|orchestral|"
                     r"sped\s*up|slowed|reverb|nightcore|8d|a\s*cappella|acapella|demo|mashup|"
                     r"re-?recorded|session|unplugged|lofi|lo-fi|edit|version|mix)\b", re.I)

def same_recording_title(want: str, cand: str) -> bool:
    if ntitle(want) == ntitle(cand):
        return True
    if not (title_matches(want, cand) and title_matches(cand, want)):
        return False
    extra = {m.group(0).lower() for m in VERSION.finditer(cand)} - {m.group(0).lower() for m in VERSION.finditer(want)}
    return not extra

def _words(s: str) -> set:
    import unicodedata
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return {w for w in re.split(r"[^a-z0-9]+", s) if w}

def deezer_identify(artist: str, title: str, video_title: str = "", channel: str = "") -> list[dict]:
    """Deezer tracks that ARE this title by this artist: the same recording's title
    (version words must agree) and a shared artist. Exact titles rank first.

    A video's credit line is often too messy to parse ("BAD BITCH( Honey Bees) x
    DJ 113 FT JD - TON MEC" is the artist "BAD BITCH JD DJ 113"), so when the
    parsed artist finds nothing, a candidate also counts when every word of its
    artist appears in the video's own title or channel."""
    out = _deezer_identify(artist, title)
    if out or not video_title:
        return out
    seen_words = _words(video_title) | _words(channel)
    for q in (f"{title} {artist}", title):
        try:
            data = _json("https://api.deezer.com/search?limit=15&q=" + urllib.parse.quote(q)).get("data") or []
        except Exception:
            continue
        for c in data:
            aw = {w for w in _words((c.get("artist") or {}).get("name", "")) if len(w) > 1}
            if aw and aw <= seen_words and same_recording_title(title, c.get("title") or "") \
                    and c.get("id") not in {x.get("id") for x in out}:
                out.append(c)
        if out:
            break
    return sorted(out, key=lambda c: ntitle(c.get("title") or "") != ntitle(title))

def _deezer_identify(artist: str, title: str) -> list[dict]:
    out, seen = [], set()
    for q in (f'artist:"{artist}" track:"{title}"', f"{artist} {title}"):
        try:
            data = _json("https://api.deezer.com/search?limit=10&q=" + urllib.parse.quote(q)).get("data") or []
        except Exception:
            continue
        for c in data:
            if c.get("id") in seen:
                continue
            seen.add(c.get("id"))
            ct = c.get("title") or ""
            if not same_recording_title(title, ct):
                continue
            if not (atoks(artist) & atoks((c.get("artist") or {}).get("name", ""))):
                continue
            out.append(c)
        if out:
            break
    return sorted(out, key=lambda c: ntitle(c.get("title") or "") != ntitle(title))

_TRACKS: dict = {}

def deezer_track(tid) -> dict:
    if tid not in _TRACKS:
        try:
            _TRACKS[tid] = _json(f"https://api.deezer.com/track/{tid}")
        except Exception:
            _TRACKS[tid] = {}
    return _TRACKS[tid]

def original_release(hits: list[dict]) -> dict:
    """The same recording sits on the single, the album and every compilation that
    reissued it; Deezer ranks them however it likes. The earliest release is the
    original — the album and year the app should show."""
    dated = [(deezer_track(c["id"]).get("release_date") or "9999", i, c) for i, c in enumerate(hits[:6])]
    return min(dated)[2]

def deezer_row(c: dict) -> dict:
    """A full row from one Deezer search hit (+ its track record for every artist)."""
    full = deezer_track(c["id"])
    names = [x.get("name") for x in full.get("contributors") or [] if x.get("name")]
    names = names or [(c.get("artist") or {}).get("name", "")]
    alb = c.get("album") or {}
    r = blank()
    r.update({
        "Track URI": f"deezer:track:{c['id']}",
        "Track Name": c.get("title") or "",
        "Artist Name(s)": ";".join(dict.fromkeys(n for n in names if n)),
        "Album Name": alb.get("title") or "",
        "Release Date": full.get("release_date") or "",
        "Duration (ms)": int(c["duration"]) * 1000 if c.get("duration") else "",
        "Explicit": str(bool(c.get("explicit_lyrics"))).lower(),
        "Cover URL": alb.get("cover_xl") or alb.get("cover_big") or "",
        "Cover Source": "deezer",
    })
    return deezer_fill(r)

# ── YouTube ──────────────────────────────────────────────────────────────────
_NOISE = re.compile(
    r"\s*[\(\[](?:official\s*(?:music\s*)?(?:video|audio|visuali[sz]er|lyric\s*video)|clip\s*officiel|"
    r"audio(?:\s*officiel)?|lyrics?|paroles|visuali[sz]er|hd|hq|4k|music\s*video|video\s*officielle)"
    r"[^\)\]]*[\)\]]", re.I)

def parse_video_title(video_title: str, channel: str) -> tuple[str, list[str]]:
    """'Artist - Title (Official Video)' → ('Title', ['Artist']). Keeps (feat. …)."""
    t = _NOISE.sub("", video_title or "").strip()
    t = re.sub(r"\s*\|\s*.*$", "", t).strip()                    # "Title | Label Session"
    for sep in (" - ", " – ", " — "):
        if sep in t:
            a, _, ti = t.partition(sep)
            names = re.split(r"\s+(?:x|&|feat\.?|ft\.?|featuring)\s+|,\s*", a, flags=re.I)
            # "BAD BITCH( Honey Bees)": an aside inside a name is not part of it
            names = [re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*", " ", x).strip() for x in names]
            return ti.strip(" -"), [x for x in names if x]
    ch = re.sub(r"\s*-\s*topic$|vevo$|\s*officiel$", "", (channel or "").strip(), flags=re.I).strip()
    return t, ([ch] if ch else [])

class YouTubeRefused(RuntimeError):
    pass

def yt_info(vid: str, port: int) -> dict:
    """Read on the VPS: its yt-dlp is current, the Mac's Python (3.9) is stuck on a
    yt-dlp that YouTube now answers with "The page needs to be reloaded"."""
    import replicate_line
    r = replicate_line.info(f"https://www.youtube.com/watch?v={vid}", port)
    if not r.get("ok"):
        raise YouTubeRefused(r.get("error") or "no metadata")
    return r["info"]

def yt_oembed(vid: str) -> dict:
    u = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(f"https://www.youtube.com/watch?v={vid}")
    return _json(u)

def youtube_video_row(vid: str, port: int) -> dict:
    url = f"https://www.youtube.com/watch?v={vid}"
    gated = False
    try:
        info = yt_info(vid, port)
    except YouTubeRefused as e:
        msg = str(e)
        if not re.search(r"confirm your age|age.restricted|sign in", msg, re.I):
            raise IntakeError(f"YouTube refuse cette vidéo : {msg.splitlines()[-1][:140]}")
        gated = True
        try:
            o = yt_oembed(vid)
        except Exception:
            raise IntakeError("vidéo soumise à une limite d'âge et titre illisible")
        info = {"title": o.get("title"), "channel": o.get("author_name"),
                "thumbnail": o.get("thumbnail_url"), "duration": None}

    channel = info.get("channel") or info.get("uploader") or ""
    yt_artists = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
    if info.get("track") and yt_artists:                      # YouTube Music's own credits
        title, artists, how = info["track"], [a.strip() for a in ", ".join(yt_artists).split(",") if a.strip()], "YouTube Music"
    else:
        title, artists, how = *parse_video_title(info.get("title") or "", channel), "titre de la vidéo"
    if not title or not artists:
        raise IntakeError(f"impossible de lire artiste/titre dans « {info.get('title')} »")
    dur = float(info["duration"]) if info.get("duration") else None

    hits = deezer_identify(artists[0], title, info.get("title") or "", channel)
    if hits and dur is not None:
        exact = [c for c in hits if ntitle(c.get("title") or "") == ntitle(title)] or hits
        fits = [c for c in exact if abs((c.get("duration") or 0) - dur) <= DURATION_TOLERANCE_S]
        best = original_release(fits) if fits else min(exact, key=lambda c: abs((c.get("duration") or 0) - dur))
        delta = abs((best.get("duration") or 0) - dur)
        r = deezer_row(best)
        if delta <= DURATION_TOLERANCE_S:                       # outcome 1: this video is the master
            r.update(pin(url, info, dur))
            r["Intake Note"] = f"identifié par Deezer, ta vidéo sert de source (Δ{delta:.0f}s)"
        else:                                                   # outcome 2: another cut
            r["Intake Note"] = (f"identifié par Deezer ; ta vidéo dure {dur:.0f}s contre {best['duration']}s "
                                f"pour le titre : on cherche la version originale")
        return r
    if hits and gated:                                          # can't read the runtime: let resolve choose
        r = deezer_row(original_release(hits))
        r["Intake Note"] = "vidéo limitée par l'âge : identifiée par Deezer, on cherche une version ouverte"
        return r
    if gated:
        raise IntakeError("vidéo limitée par l'âge et inconnue de Deezer : envoie un autre lien de ce titre")

    r = blank()                                                 # outcome 3: only YouTube knows it
    rel = info.get("release_date") or ""
    r.update({
        "Track URI": f"youtube:{vid}",
        "Track Name": title,
        "Artist Name(s)": ";".join(artists),
        "Album Name": info.get("album") or "",
        # YouTube Music's release date is the release's; an upload date is not, so it isn't used.
        "Release Date": f"{rel[:4]}-{rel[4:6]}-{rel[6:8]}" if re.fullmatch(r"\d{8}", rel)
                        else (str(info["release_year"]) if info.get("release_year") else ""),
        "Duration (ms)": int(dur * 1000) if dur else "",
        "Cover URL": info.get("thumbnail") or "",
        "Cover Source": "youtube",
        "Intake Note": f"inconnu de Deezer : identité tirée de {how}",
    })
    r.update(pin(url, info, dur))
    return r

def pin(url: str, info: dict, dur: float | None) -> dict:
    return {"Source URL": url, "Source Title": info.get("title") or "",
            "Source Channel": info.get("channel") or info.get("uploader") or "",
            "Source Duration (s)": f"{dur:.1f}" if dur else ""}

def youtube_rows(kind: str, yid: str) -> tuple[list[dict], list[str]]:
    import replicate_line
    replicate_line.push()
    with replicate_line.Tunnel() as port:
        return _youtube_rows(kind, yid, port)

def _youtube_rows(kind: str, yid: str, port: int) -> tuple[list[dict], list[str]]:
    if kind == "video":
        return [youtube_video_row(yid, port)], []
    import replicate_line
    pl = replicate_line.info(f"https://www.youtube.com/playlist?list={yid}", port, playlist=True)
    vids = pl.get("ids") or []
    if not vids:
        raise IntakeError("playlist vide ou privée")
    if len(vids) > MAX_TRACKS:
        raise IntakeError(f"{len(vids)} vidéos : au-delà de la limite de {MAX_TRACKS} par lien")
    rows, notes = [], []

    def one(v):
        try:
            return youtube_video_row(v, port), None
        except IntakeError as e:
            return None, f"{v} : {e}"
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        for r, err in pool.map(one, vids):
            (rows.append(r) if r else notes.append(err))
    if not rows:
        raise IntakeError("aucune vidéo de la playlist n'a pu être identifiée")
    return rows, notes

# ── entry point ─────────────────────────────────────────────────────────────
def blank() -> dict:
    return {c: "" for c in COLUMNS}

def expand(url: str) -> tuple[list[dict], list[str]]:
    """A link → (rows, notes). Raises IntakeError with a message fit for the page."""
    provider, kind, xid = classify(url)
    rows, notes = (spotify_rows if provider == "spotify" else youtube_rows)(kind, xid)
    # One link can name the same recording twice (a playlist with a dupe).
    seen, out = set(), []
    for r in rows:
        k = (ntitle(r["Track Name"]), frozenset(atoks(r["Artist Name(s)"])))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out, notes

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('usage: link_intake.py "<spotify or youtube link>"')
    try:
        rows, notes = expand(sys.argv[1])
    except IntakeError as e:
        sys.exit(f"  ✗ {e}")
    for r in rows:
        print(f"  {r['Artist Name(s)'].replace(';', ', ')} — {r['Track Name']}"
              f"  [{int(r['Duration (ms)'] or 0)/1000:.0f}s, {r['Album Name'] or '-'}, {r['Release Date'] or '-'},"
              f" label {r['Record Label'] or '-'}]")
        print(f"      {r['Intake Note']}" + (f"  · source épinglée {r['Source URL']}" if r["Source URL"] else ""))
    for n in notes:
        print(f"  ! {n}")
