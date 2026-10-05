#!/usr/bin/env python3
"""
romaji.py — turn a Japanese song title into romaji, safely.

Shared by the catalogue fixer and by ingest_gpu.py, so new Japanese-titled songs
are romanised at ingest without anyone having to ask.

WHY THIS ISN'T JUST pykakasi
  Kanji readings are ambiguous and loanwords round-trip badly:
    君      -> "Kun"  where the song means "Kimi"
    ラヴ    -> "Ravu" where the word is "Love"
    土曜    -> "Do You" if segmented wrong, vs "Doyou"
  So an official romanisation is preferred: Genius often titles a song
  「漢字 (Romanised)」 and iTunes frequently carries a romanised release.
  pykakasi is the last resort, not the first.

WHY THE GUARDRAILS EXIST
  Taking a source's string blindly is worse than transliterating. Real results
  seen on this catalogue before these checks:
    黄昏のBAY CITY   -> "Romanized"       (a lyrics-translation page's title)
    接吻             -> "Album Version"   (a version qualifier, not a title)
    内気なジュリエット -> "Juliet"          (dropped most of the title)
  Anything that is only a qualifier, or that throws away most of the title, is
  rejected and we fall through to the next source.
"""
from __future__ import annotations
import json, re, time, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CJK = re.compile(r"[぀-ゟ゠-ヿ㐀-鿿豈-﫿ｦ-ﾟ]")

# Strings that are page/version labels rather than a song title.
JUNK = re.compile(
    r"^(romanized|romanization|romaji|english( translation)?|translation|traduction|"
    r"traduzione( italiana)?|traduccion|traducci\u00f3n|\u00fcbersetzung|deutsche \u00fcbersetzung|"
    r"polskie tlumaczenie|tradu\u00e7\u00e3o|lyrics|album version|single version|live|remix|"
    r"instrumental|karaoke|remaster(ed)?|\d{4} remaster(ed)?|version|audio|full album)$", re.I)

def has_japanese(s: str) -> bool:
    return bool(CJK.search(s or ""))

def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s.strip(" -–—|·・")

def _plausible(candidate: str, original: str, artist: str = "") -> bool:
    """Is this a real romanisation of THIS title, or a stray label?"""
    c = _clean(candidate)
    # "\u63a5\u543b" came back as "ORIGINAL LOVE, 1993" \u2014 a Genius page header naming the
    # artist and year. A romanised TITLE does not consist of the artist's name.
    if artist:
        a = re.sub(r"[^a-z0-9]", "", artist.lower())
        cc = re.sub(r"[^a-z0-9]", "", c.lower())
        if a and cc and (cc.startswith(a) or a.startswith(cc)):
            return False
    if not c or len(c) < 2:
        return False
    if JUNK.match(c):
        return False
    if CJK.search(c):                      # still Japanese: not a romanisation
        return False
    if not re.search(r"[A-Za-z]", c):
        return False
    # A title of several Japanese characters shouldn't collapse to one short word
    # ("内気なジュリエット" -> "Juliet" drops the 内気な entirely).
    jp_chars = len(CJK.findall(original))
    if jp_chars >= 5 and len(c.split()) < 2 and len(c) < 8:
        return False
    return True

def _itunes(term: str, limit: int = 5) -> list:
    u = (f"https://itunes.apple.com/search?term={urllib.parse.quote(term)}"
         f"&entity=song&limit={limit}")
    for _ in range(2):
        try:
            return json.loads(urllib.request.urlopen(u, timeout=20).read()
                              .decode("utf-8", "replace")).get("results", [])
        except Exception:
            time.sleep(1.2)
    return []

def _genius_romaji(artist: str, title: str) -> str | None:
    tok_path = HERE / ".genius_token"
    if not tok_path.exists():
        return None
    try:
        tok = tok_path.read_text(encoding="utf-8").strip()
        u = "https://api.genius.com/search?q=" + urllib.parse.quote(f"{artist} {title}")
        r = urllib.request.Request(u, headers={"Authorization": f"Bearer {tok}"})
        hits = json.loads(urllib.request.urlopen(r, timeout=20).read())["response"]["hits"]
    except Exception:
        return None
    key = title[:3]
    for h in hits[:5]:
        t = h["result"].get("title") or ""
        if key and key not in t:
            continue
        m = re.search(r"[（(]\s*([^()（）]+?)\s*[)）]\s*$", t)
        if m and _plausible(m.group(1), title, artist):
            return _clean(m.group(1))
    return None

def _itunes_romaji(artist: str, title: str) -> str | None:
    for r in _itunes(f"{artist} {title}"):
        tn = r.get("trackName") or ""
        if _plausible(tn, title, artist):
            # keep it only if it's plausibly the same song, not a neighbour track
            tn = re.sub(r"\s*[\(\[](feat|ft)\.?[^)\]]*[\)\]]", "", tn, flags=re.I)
            # iTunes often returns a specific pressing ("... (Live at Budokan)",
            # "... [2012 Remaster]"). Keep such a marker ONLY if the original
            # title carried one too, otherwise we'd invent a version.
            if not re.search(r"[\(\[]", title):
                tn = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", tn)
            return _clean(tn)
    return None

def _kakasi_romaji(title: str) -> str | None:
    try:
        import pykakasi
    except ImportError:
        return None
    try:
        conv = pykakasi.kakasi().convert(title)
        s = " ".join(x["hepburn"] for x in conv)
        s = re.sub(r"\s*([・、。])\s*", " ", s)
        s = re.sub(r"\s*([,.!?])\s*", r"\1 ", s)
        s = re.sub(r"\s+", " ", s).strip()
        s = " ".join(w.capitalize() if w.islower() else w for w in s.split())
        return _clean(s) or None
    except Exception:
        return None

def to_romaji(title: str, artist: str = "", allow_network: bool = True) -> tuple[str, str]:
    """Return (romaji_title, source). Non-Japanese titles come back unchanged.

    Any Latin text already in the title (e.g. "黄昏のBAY CITY") is preserved by
    the transliterator, so mixed titles don't lose their English half.
    """
    if not has_japanese(title):
        return title, "unchanged"
    if allow_network:
        for fn, name in ((_genius_romaji, "genius"), (_itunes_romaji, "itunes")):
            try:
                got = fn(artist, title)
            except Exception:
                got = None
            if got:
                return got, name
    got = _kakasi_romaji(title)
    if got and _plausible(got, title):
        return got, "pykakasi"
    return title, "failed"


_MB_UA = "ZingyCatalog/1.0 (dimemoctar@gmail.com)"
_mb_last = [0.0]

def _flip_sortname(n: str) -> str:
    """MusicBrainz returns artists in sort form ("Mamiya, Takako"). The catalogue
    writes people the way they are credited — "Takako Mamiya"."""
    if n.count(",") == 1:
        last, first = [x.strip() for x in n.split(",")]
        if last and first:
            return f"{first} {last}"
    return n

def _latin_artist(name: str) -> str | None:
    """The official Latin spelling of a Japanese artist, from MusicBrainz.

    Transliteration is wrong for artist names in two ways at once: pykakasi
    renders 大滝詠一 surname-first as "Ootaki Eiichi" where the world writes
    "Eiichi Ohtaki", and it spells サザンオールスターズ out phonetically as
    "Sazan'oorusutaazu" when the band is simply "Southern All Stars".
    MusicBrainz stores Latin aliases explicitly, which is why it already
    resolved 山口美央子 -> Mioko Yamaguchi for this catalogue.

    Rate-limited to MusicBrainz's 1 req/sec: it answers 503 to bursts, and a
    503 silently swallowed is how a lookup "fails" for no real reason.
    """
    for attempt in range(3):
        wait = 1.15 - (time.time() - _mb_last[0])
        if wait > 0:
            time.sleep(wait)
        _mb_last[0] = time.time()
        try:
            u = ("https://musicbrainz.org/ws/2/artist?"
                 + urllib.parse.urlencode({"query": name, "fmt": "json", "limit": 5}))
            req = urllib.request.Request(u, headers={"User-Agent": _MB_UA})
            data = json.loads(urllib.request.urlopen(req, timeout=25).read())
            break
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    else:
        return None
    def ok(x):
        return bool(x) and bool(re.search(r"[A-Za-z]", x)) and not has_japanese(x)
    for a in data.get("artists", []):
        # a Latin alias is the artist's own preferred spelling; sort-name is a fallback
        for al in (a.get("aliases") or []):
            n = al.get("name")
            if ok(n) and "," not in n:
                return n.strip()
        for key in ("name", "sort-name"):
            n = a.get(key)
            if ok(n):
                return _flip_sortname(n).strip()
    return None

def romanize_artist(artist: str) -> tuple[str, str]:
    """Romanise an ARTIST field, preserving its ';'/',' collaborator structure.

    Titles were romanised first and artists were missed; the requirement is that
    no Japanese script reaches the app at all, which includes the byline.

    Most catalogue entries already carry the answer inline — Genius writes
    「大滝詠一」 but also 「重音テト (Kasane Teto)」 — so a parenthetical Latin
    reading is taken as authoritative before anything is transliterated.
    """
    if not has_japanese(artist):
        return artist, "unchanged"
    parts = re.split(r"\s*;\s*", artist)
    out, srcs = [], []
    for part in parts:
        subs = re.split(r"\s*,\s*", part)
        done = []
        for p in subs:
            if not has_japanese(p):
                done.append(p); continue
            m = re.search(r"[（(]\s*([A-Za-z][^()（）]*?)\s*[)）]", p)
            if m:
                done.append(m.group(1).strip()); srcs.append("inline")
                continue
            lat = _latin_artist(p)
            if lat:
                done.append(lat); srcs.append("musicbrainz")
                continue
            r, s = to_romaji(p, "")
            done.append(r if r and not has_japanese(r) else p)
            srcs.append(s)
        out.append(", ".join(done))
    joined = "; ".join(out)
    return joined, ("inline" if "inline" in srcs else (srcs[0] if srcs else "failed"))

if __name__ == "__main__":
    import sys
    for t in sys.argv[1:]:
        print(t, "->", to_romaji(t))
