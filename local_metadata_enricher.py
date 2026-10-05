#!/usr/bin/env python3
"""Metadata enricher for the catalogue rows on the VPS (Mac-side, residential IP).

Fills producers / writers / year / label / release info on every ready catalogue
track, and applies the cover policy: Genius song art becomes the PRIMARY cover,
the previous (Spotify/Apple) cover is preserved in cover_url_fallback — the app
registers that pair and falls back automatically if the Genius image 404s.

What was wrong with the old version (and is fixed here):
  * It used the unofficial genius.com/api → IP-banned → silent misses → 85% of
    the catalogue had no producers. Now: official api.genius.com via meta_sources.
  * It stamped meta_analyzed=true even when every field came back NULL, so
    nothing was ever retried. Now: selection is BY MISSING FIELDS, and a local
    attempts file (enrich_attempts.json) caps retries at 3 per track instead.
  * A found-nothing pass could overwrite good values with NULL. Now every SQL
    update is COALESCE-guarded — existing data is never clobbered.

Usage:
    python3 local_metadata_enricher.py               # enrich everything missing
    python3 local_metadata_enricher.py --limit 20    # small test batch
    python3 local_metadata_enricher.py --force       # ignore the attempts cap
    python3 local_metadata_enricher.py --dry-run     # show, don't write
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time

from meta_sources import enrich, save_cache

VPS = "204.168.236.140"
HERE = os.path.dirname(os.path.abspath(__file__))
ATTEMPTS_PATH = os.path.join(HERE, "enrich_attempts.json")
MAX_ATTEMPTS = 3
FLUSH_EVERY = 40

CORE = ("producers", "writers", "year", "record_label")


def psql(sql: str) -> str:
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=12", f"root@{VPS}",
                        f"sudo -u postgres psql -d zingy -tA -c \"{sql}\""],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"psql failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def psql_batch(statements: list[str]) -> None:
    tmp = "/tmp/enrich_updates.sql"
    open(tmp, "w", encoding="utf-8").write("\n".join(statements))
    subprocess.run(f"cat {tmp} | ssh root@{VPS} \"sudo -u postgres psql -d zingy -q\"",
                   shell=True, check=True)
    os.remove(tmp)


def esc(v) -> str:
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def fetch_incomplete(ids: list[str] | None = None) -> list[dict]:
    scope = ""
    if ids:
        scope = "AND id IN (" + ",".join(f"'{i}'" for i in ids) + ") "
    q = ("SELECT row_to_json(r) FROM (SELECT id,title,artist,producers,writers,year,"
         "record_label,cover_url,cover_url_fallback,album,release_type,release_date,genres "
         "FROM tracks WHERE status='ready' "
         "AND owner_id=(SELECT id FROM users WHERE username='__catalog__') "
         + scope +
         "AND (producers IS NULL OR producers='' OR writers IS NULL OR writers='' "
         "OR year IS NULL OR record_label IS NULL OR record_label='' "
         "OR genres IS NULL OR genres='' "
         "OR cover_url IS NULL OR cover_url NOT LIKE '%images.genius.com%') "
         "ORDER BY created_at DESC) r")
    return [json.loads(l) for l in psql(q).splitlines() if l.strip()]


def build_update(t: dict, m: dict) -> tuple[str | None, bool]:
    """→ (sql or None, now_complete). COALESCE keeps existing values authoritative."""
    sets = []
    if m.get("producers"):
        sets.append(f"producers=COALESCE(NULLIF(producers,''), {esc(m['producers'])})")
    if m.get("writers"):
        sets.append(f"writers=COALESCE(NULLIF(writers,''), {esc(m['writers'])})")
    if m.get("featuring"):
        sets.append(f"featuring=COALESCE(NULLIF(featuring,''), {esc(m['featuring'])})")
    if m.get("year"):
        sets.append(f"year=COALESCE(year, {int(m['year'])})")
    if m.get("label"):
        sets.append(f"record_label=COALESCE(NULLIF(record_label,''), {esc(m['label'])})")
    if m.get("release_date") and len(str(m["release_date"])) >= 7:
        rd = str(m["release_date"])
        rd = rd if len(rd) >= 10 else rd + "-01"          # YYYY-MM → a valid DATE
        sets.append(f"release_date=COALESCE(release_date, {esc(rd)})")
    if m.get("album"):
        sets.append(f"album=COALESCE(NULLIF(album,''), {esc(m['album'])})")
    if m.get("release_type"):
        sets.append(f"release_type=COALESCE(NULLIF(release_type,''), {esc(m['release_type'])})")
    if m.get("genres"):
        g_str = ",".join(m["genres"]) if isinstance(m["genres"], list) else str(m["genres"])
        sets.append(f"genres=COALESCE(NULLIF(genres,''), {esc(g_str)})")
    # Cover policy: Genius art → primary, previous cover → fallback (kept once).
    art = m.get("art_genius")
    if art and art != t.get("cover_url"):
        sets.append(f"cover_url_fallback=COALESCE(cover_url_fallback, NULLIF(cover_url,''))")
        sets.append(f"cover_url={esc(art)}")
    elif not t.get("cover_url") and m.get("art_deezer"):
        sets.append(f"cover_url={esc(m['art_deezer'])}")

    have = {
        "producers": t.get("producers") or m.get("producers"),
        "writers": t.get("writers") or m.get("writers"),
        "year": t.get("year") or m.get("year"),
        "record_label": t.get("record_label") or m.get("label"),
        "genres": t.get("genres") or m.get("genres"),
    }
    complete = all(have.values())
    if complete:
        sets.append("meta_analyzed=true")
    if not sets:
        return None, complete
    return f"UPDATE tracks SET {', '.join(sets)} WHERE id='{t['id']}';", complete


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true", help="ignore the attempts cap")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ids-file", help="only enrich these track ids (one per line) — "
                                       "used by auto_ingest to scope to its batch")
    a = ap.parse_args()

    try:
        attempts = json.load(open(ATTEMPTS_PATH, encoding="utf-8"))
    except Exception:
        attempts = {}

    ids = None
    if a.ids_file:
        ids = [l.strip() for l in open(a.ids_file, encoding="utf-8") if l.strip()]
        if not ids:
            print("[enrich] ids file empty — nothing to do")
            return
    rows = fetch_incomplete(ids)
    if not a.force:
        skipped = [t for t in rows if attempts.get(t["id"], 0) >= MAX_ATTEMPTS]
        rows = [t for t in rows if attempts.get(t["id"], 0) < MAX_ATTEMPTS]
        if skipped:
            print(f"[enrich] {len(skipped)} track(s) hit the {MAX_ATTEMPTS}-attempt cap "
                  f"(unfindable) — skipped; use --force to retry them")
    if a.limit:
        rows = rows[: a.limit]
    print(f"[enrich] {len(rows)} track(s) with missing metadata")
    if not rows:
        return

    pending, applied, completed = [], 0, 0
    for i, t in enumerate(rows, 1):
        title, artist = t["title"], t.get("artist") or ""
        print(f"[{i}/{len(rows)}] {artist} — {title}")
        m = enrich(artist, title)
        sql, complete = build_update(t, m)
        got = [k for k in ("producers", "writers", "year", "label") if m.get(k)]
        print(f"    got: {', '.join(got) or 'nothing'}  "
              f"(via {'+'.join(m['sources']) or 'no source'})"
              f"{'  ✓ complete' if complete else ''}")
        attempts[t["id"]] = attempts.get(t["id"], 0) + 1
        if complete:
            completed += 1
        if sql:
            pending.append(sql)
        if len(pending) >= FLUSH_EVERY and not a.dry_run:
            psql_batch(pending)
            applied += len(pending)
            pending = []
            json.dump(attempts, open(ATTEMPTS_PATH, "w", encoding="utf-8"))
            save_cache()
            print(f"    [flushed {applied} updates so far]")
        time.sleep(0.2)

    if pending and not a.dry_run:
        psql_batch(pending)
        applied += len(pending)
    json.dump(attempts, open(ATTEMPTS_PATH, "w", encoding="utf-8"))
    save_cache()
    print(f"\n[enrich] done — {applied} row(s) updated, {completed} now fully complete"
          + (" (dry-run: nothing written)" if a.dry_run else ""))


if __name__ == "__main__":
    main()
