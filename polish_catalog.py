#!/usr/bin/env python3
"""Final polish pass on the ready CATALOG tracks (residential IP, Mac):
   1) clean dirty YouTube titles (annotations / handles / album spam)
   2) backfill missing cover art (Genius song art -> Deezer album cover)
Writes a batched SQL UPDATE to the VPS."""
import json, re, subprocess, time

from meta_sources import genius_song, deezer_info, save_cache

VPS = "204.168.236.140"


def clean_title(t):
    if not t:
        return t
    s = t
    # 1. Remove "A COLORS SHOW" (with optional separators like |, /, -, or just spaces)
    s = re.sub(r'\s*([\|/–—-]+)?\s*\bA\s*COLORS\s*SHOW\b.*$', '', s, flags=re.I)
    
    # 2. Remove comments/album spam like "// album // year"
    s = re.sub(r'\s*//.*$', '', s)
    
    # 3. Remove bracketed annotations: (Official Audio), [Visualizer], etc.
    s = re.sub(r'\s*[\(\[][^)\]]*\b(official|clip|video|audio|lyric|visualizer|performance|live session|session|colors)\b[^)\]]*[\)\]]', '', s, flags=re.I)
    
    # 4. Remove bare suffixes with separators (e.g., " - Official Video", " - Visualizer", " - Audio")
    pat_bare = r'\s*([\|/–—-]+)\s*\b(official video|music video|official audio|audio|video clip|lyric video|lyrics|visualizer|official|officiel|clip|live session|live|performance)\b.*$'
    s = re.sub(pat_bare, '', s, flags=re.I)
    
    # 5. Remove trailing bare words (e.g. "MISS KITOKO VISUALIZER" -> "MISS KITOKO")
    pat_trailing = r'\s+\b(visualizer|official video|music video|official audio|audio|video clip|lyric video|lyrics|clip officiel|clip|official|officiel)\b\s*$'
    s = re.sub(pat_trailing, '', s, flags=re.I)
    
    # 6. Remove standard trailing artifacts like M/V, MV
    s = re.sub(r'\s*\bM/?V\b\s*$', '', s, flags=re.I)
    
    # 7. Remove handles like @username
    s = re.sub(r'\s*@\w+', '', s)
    
    # 8. If there's a '|' (vertical pipe), and the left side is long enough, drop the right side
    s = re.sub(r'\s*[\|]\s*.*$', '', s) if '|' in s and len(s.split('|')[0].strip()) > 2 else s
    
    # 9. Clean spacing and trailing punctuation
    s = re.sub(r'\s{2,}', ' ', s).strip(" -–—|/")
    return s.strip() or t


def cover_genius(title, artist):
    """Official API via meta_sources — the unofficial genius.com/api IP-bans."""
    g = genius_song(artist, title)
    return (g or {}).get("art")


def cover_deezer(title, artist):
    dz = deezer_info(artist, title)
    return (dz or {}).get("art")


def esc(v):
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def main():
    q = ("SELECT row_to_json(r) FROM (SELECT id,title,artist,cover_url FROM tracks "
         "WHERE status='ready' AND owner_id=(SELECT id FROM users WHERE username='__catalog__')) r;")
    out = subprocess.run(["ssh", f"root@{VPS}", f"sudo -u postgres psql -d zingy -tA -c \"{q}\""],
                         capture_output=True, text=True, check=True).stdout
    rows = [json.loads(l) for l in out.strip().splitlines() if l.strip()]
    print(f"{len(rows)} ready catalog tracks")
    sql = []
    for i, t in enumerate(rows, 1):
        sets = []
        ct = clean_title(t["title"])
        if ct != t["title"]:
            sets.append(f"title={esc(ct)}")
        if not t.get("cover_url"):
            art = cover_genius(t["title"], t["artist"]) or cover_deezer(t["title"], t["artist"])
            if art:
                sets.append(f"cover_url={esc(art)}")
            time.sleep(0.3)
        if sets:
            sql.append(f"UPDATE tracks SET {', '.join(sets)} WHERE id='{t['id']}';")
            print(f"[{i}] {t['title'][:40]} -> {', '.join(s.split('=')[0] for s in sets)}")
    if not sql:
        print("nothing to update"); return
    open("/tmp/polish.sql", "w").write("\n".join(sql))
    subprocess.run(f"cat /tmp/polish.sql | ssh root@{VPS} \"sudo -u postgres psql -d zingy\"",
                   shell=True, check=True)
    save_cache()
    print(f"applied {len(sql)} updates")


if __name__ == "__main__":
    main()
