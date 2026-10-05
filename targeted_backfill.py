#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess, sys, time
from meta_sources import enrich, save_cache

VPS = "204.168.236.140"

def psql(sql: str) -> str:
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=12", f"root@{VPS}",
                        f"sudo -u postgres psql -d zingy -tA -c \"{sql}\""],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"psql failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()

def psql_batch(statements: list[str]) -> None:
    if not statements: return
    tmp = "/tmp/enrich_updates.sql"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(statements))
    subprocess.run(f"cat {tmp} | ssh root@{VPS} \"sudo -u postgres psql -d zingy -q\"",
                   shell=True, check=True)
    os.remove(tmp)

def esc(v) -> str:
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"

def fetch_missing_targets() -> list[dict]:
    # We target tracks missing cover_url, cover_url_fallback, or genres.
    q = ("SELECT row_to_json(r) FROM (SELECT id,title,artist,cover_url,cover_url_fallback,genres "
         "FROM tracks WHERE status='ready' "
         "AND owner_id=(SELECT id FROM users WHERE username='__catalog__') "
         "AND (genres IS NULL OR genres='' OR cover_url_fallback IS NULL OR cover_url_fallback='' OR cover_url IS NULL OR cover_url='')) r")
    return [json.loads(l) for l in psql(q).splitlines() if l.strip()]

def build_update(t: dict, m: dict) -> str | None:
    sets = []
    if m.get("genres") and not t.get("genres"):
        g_str = ",".join(m["genres"]) if isinstance(m["genres"], list) else str(m["genres"])
        sets.append(f"genres={esc(g_str)}")
    
    art = m.get("art_genius")
    if art and art != t.get("cover_url"):
        # We got a new genius cover. The old cover goes to fallback.
        sets.append(f"cover_url_fallback=COALESCE(NULLIF(cover_url_fallback, ''), NULLIF(cover_url,''))")
        sets.append(f"cover_url={esc(art)}")
    elif not t.get("cover_url"):
        if m.get("art_deezer"):
            sets.append(f"cover_url={esc(m['art_deezer'])}")
        elif m.get("art_itunes"):
            sets.append(f"cover_url={esc(m['art_itunes'])}")
            
    # If we still have no fallback, but we have deezer/itunes art, put it in fallback
    if not t.get("cover_url_fallback") and not any(s.startswith("cover_url_fallback") for s in sets):
        if m.get("art_deezer") and m.get("art_deezer") != art:
            sets.append(f"cover_url_fallback={esc(m['art_deezer'])}")
        elif m.get("art_itunes") and m.get("art_itunes") != art:
            sets.append(f"cover_url_fallback={esc(m['art_itunes'])}")

    if not sets:
        return None
    return f"UPDATE tracks SET {', '.join(sets)} WHERE id='{t['id']}';"

def main():
    rows = fetch_missing_targets()
    print(f"Found {len(rows)} tracks missing target metadata.")
    
    pending, applied = [], 0
    for i, t in enumerate(rows, 1):
        title, artist = t["title"], t.get("artist") or ""
        print(f"[{i}/{len(rows)}] {artist} - {title}", end=" -> ")
        m = enrich(artist, title)
        sql = build_update(t, m)
        if sql:
            pending.append(sql)
            print("UPDATED")
        else:
            print("No new data")
            
        if len(pending) >= 40:
            psql_batch(pending)
            applied += len(pending)
            pending = []
            save_cache()
            print(f"    [flushed {applied} updates so far]")
        time.sleep(0.2)

    if pending:
        psql_batch(pending)
        applied += len(pending)
    save_cache()
    print(f"Done. {applied} rows updated.")

if __name__ == "__main__":
    main()
