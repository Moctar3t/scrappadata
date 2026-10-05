#!/usr/bin/env python3
"""Read-only audit of every song JSON shipped in the iOS app.

Reports per-field completeness, release-format coverage, stem-host breakdown,
and which songs are DB-backed (catalog) vs legacy (hand-curated, not in VPS DB).
Writes app_audit_report.json and prints a summary. Touches nothing.
"""
import json, os, re, glob
from urllib.parse import urlparse

APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
SONGS = os.path.join(APP, "songs")
IDMAP = "/Users/dime/Developer/scrappadata/catalog_id_map.json"

def load(p, d):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return d

release_formats = load(os.path.join(APP, "release-formats.json"), {})
id_map = load(IDMAP, {})
db_numeric_ids = set(str(v) for v in id_map.values())  # numeric ids that came from DB

# index entries (for genres + hasStems cross-check)
index = load(os.path.join(APP, "music-index.json"), {})
idx_by_id = {str(s.get("id")): s for s in index.get("songsList", [])}

FIELDS = ["producers", "writers", "year", "recordLabel", "key", "bpm", "coverArt"]

rows = []
files = sorted(glob.glob(os.path.join(SONGS, "*.json")))
for fp in files:
    s = load(fp, None)
    if not s:
        rows.append({"file": os.path.basename(fp), "PARSE_ERROR": True})
        continue
    sid = str(s.get("id", ""))
    missing = []
    for f in FIELDS:
        v = s.get(f)
        if v is None or (isinstance(v, str) and not v.strip()) or v == 0 and f == "bpm":
            missing.append(f)

    stems = s.get("stems", []) or []
    stem_hosts = set()
    stem_paths = []
    for st in stems:
        u = st.get("url", "")
        if u and isinstance(u, str):
            p = urlparse(u)
            stem_hosts.add(p.netloc)
            stem_paths.append(p.path)
    stem_kind = "none"
    if any("/catalogtracks/" in p for p in stem_paths):
        stem_kind = "catalogtracks"
    elif any("/usertracks/" in p for p in stem_paths):
        stem_kind = "usertracks"
    elif stem_paths:
        stem_kind = "legacy-named"

    fmt = release_formats.get(sid, "")
    rows.append({
        "id": sid,
        "title": s.get("title", ""),
        "artist": s.get("artist", ""),
        "db_backed": sid in db_numeric_ids,
        "missing": missing,
        "stem_count": len(stems),
        "stem_kind": stem_kind,
        "stem_hosts": sorted(stem_hosts),
        "format": fmt,
        "format_missing": sid not in release_formats,
        "genres": idx_by_id.get(sid, {}).get("genres", None),
    })

json.dump(rows, open("app_audit_report.json", "w"), indent=2, ensure_ascii=False)

# ---- summary ----
n = len([r for r in rows if "PARSE_ERROR" not in r])
parse_err = [r for r in rows if r.get("PARSE_ERROR")]
db = [r for r in rows if r.get("db_backed")]
legacy = [r for r in rows if not r.get("db_backed") and "PARSE_ERROR" not in r]

def pct(x):
    return f"{x} ({100*x/n:.1f}%)" if n else str(x)

print(f"TOTAL song files: {len(rows)}   (DB-backed: {len(db)}, legacy: {len(legacy)}, parse errors: {len(parse_err)})")
print()
print("MISSING FIELD COUNTS (across all files):")
for f in FIELDS:
    c = sum(1 for r in rows if "missing" in r and f in r["missing"])
    print(f"  {f:12s}: {pct(c)}")
print()
fmt_missing = [r for r in rows if r.get("format_missing")]
single = [r for r in rows if r.get("format") == "Single"]
album = [r for r in rows if str(r.get("format","")).startswith(("Album","EP","Mixtape","Compilation"))]
print(f"RELEASE FORMAT: missing entry: {len(fmt_missing)}, 'Single': {len(single)}, album-ish: {len(album)}")
print()
from collections import Counter
sk = Counter(r.get("stem_kind") for r in rows if "stem_kind" in r)
print("STEM KIND:", dict(sk))
nostem = [r for r in rows if r.get("stem_count", 0) == 0 and "PARSE_ERROR" not in r]
partial = [r for r in rows if 0 < r.get("stem_count", 0) < 4]
print(f"  no stems: {len(nostem)},  partial (<4): {len(partial)}")
hosts = Counter()
for r in rows:
    for h in r.get("stem_hosts", []):
        hosts[h] += 1
print("  stem hosts:", dict(hosts))
print()
print("Wrote app_audit_report.json")
