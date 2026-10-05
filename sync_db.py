#!/usr/bin/env python3
"""
Sync ingest_gpu_state.json published tracks → Postgres DB on VPS.
Generates INSERT statements for tracks not yet in the DB.
"""
import json, subprocess, sys, tempfile, os, ast
from pathlib import Path

VPS = "root@204.168.236.140"
OWNER_ID = "30b305c6-5b19-43de-acc3-9ba2fa71a565"  # catalog owner
CDN_BASE = "https://dimesystemsstorage.b-cdn.net/catalogtracks"

def esc(s):
    """Escape a string for Postgres SQL."""
    if s is None:
        return "NULL"
    return "'" + str(s).replace("'", "''") + "'"

def parse_field(val):
    """Safely parse a stringified dict/list."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return ast.literal_eval(str(val))
    except:
        return None

def parse_genres(raw):
    """Extract genres as comma-separated string."""
    parsed = parse_field(raw)
    if isinstance(parsed, list):
        return ", ".join(parsed)
    if isinstance(parsed, str):
        return raw
    return None

def parse_release_date(raw):
    """Return a DATE literal or NULL."""
    if not raw:
        return "NULL"
    s = str(raw).strip()
    if len(s) == 4 and s.isdigit():
        return f"'{s}-01-01'"
    if len(s) == 10:
        return f"'{s}'"
    return "NULL"

def parse_year(raw):
    """Extract a 4-digit year integer."""
    if not raw:
        return "NULL"
    s = str(raw).strip()[:4]
    if s.isdigit():
        return s
    return "NULL"

def build_insert(t):
    uuid = t["uuid"]
    cdn = f"{CDN_BASE}/{uuid}"
    features = parse_field(t.get("features")) or {}
    genius = parse_field(t.get("genius")) or {}
    genres = parse_genres(t.get("genres"))
    producers = genius.get("producers")
    writers = genius.get("writers")
    cover_url = genius.get("coverArt")
    
    # Producers/writers: join list to string
    if isinstance(producers, list):
        producers = ", ".join(producers)
    if isinstance(writers, list):
        writers = ", ".join(writers)

    # Featuring: extract from artists list
    artists_raw = parse_field(t.get("artists")) or []
    primary = t.get("artist", "")
    featuring = None
    if isinstance(artists_raw, list) and len(artists_raw) > 1:
        feat = [a for a in artists_raw if a != primary.split(";")[0].strip()]
        if feat:
            featuring = ", ".join(feat)

    duration_sec = None
    if t.get("duration_ms"):
        duration_sec = int(t["duration_ms"]) // 1000
    elif t.get("fetched_duration"):
        duration_sec = int(t["fetched_duration"])

    cols = [
        "id", "owner_id", "title", "artist", "featuring", "producers", "writers",
        "year", "release_date", "record_label", "album",
        "bpm", "song_key", "duration_sec",
        "cover_url", "bass_url", "drums_url", "vocals_url", "other_url", "original_url",
        "visibility", "status", "catalog_id", "genres",
        "valence", "danceability", "energy", "acousticness", "speechiness", "instrumentalness",
        "created_at", "meta_analyzed"
    ]
    vals = [
        esc(uuid),                                      # id
        esc(OWNER_ID),                                  # owner_id
        esc(t.get("title")),                            # title
        esc(t.get("artist")),                           # artist
        esc(featuring),                                 # featuring
        esc(producers),                                 # producers
        esc(writers),                                   # writers
        parse_year(t.get("release_date")),              # year
        parse_release_date(t.get("release_date")),      # release_date
        esc(t.get("record_label")),                     # record_label
        esc(t.get("album")),                            # album
        str(t.get("bpm") or "NULL"),                    # bpm
        esc(t.get("key")),                              # song_key
        str(duration_sec) if duration_sec else "NULL",  # duration_sec
        esc(cover_url),                                 # cover_url
        esc(f"{cdn}/bass.mp3"),                         # bass_url
        esc(f"{cdn}/drums.mp3"),                        # drums_url
        esc(f"{cdn}/vocals.mp3"),                       # vocals_url
        esc(f"{cdn}/other.mp3"),                        # other_url
        esc(f"{cdn}/original.mp3"),                     # original_url
        "'public'",                                     # visibility
        "'ready'",                                      # status
        esc(t.get("catalog_id")),                       # catalog_id
        esc(genres),                                    # genres
        str(features.get("valence", "NULL")),           # valence
        str(features.get("danceability", "NULL")),      # danceability
        str(features.get("energy", "NULL")),            # energy
        str(features.get("acousticness", "NULL")),      # acousticness
        str(features.get("speechiness", "NULL")),       # speechiness
        str(features.get("instrumentalness", "NULL")),  # instrumentalness
        "NOW()",                                        # created_at
        "true",                                         # meta_analyzed
    ]
    return f"INSERT INTO tracks ({', '.join(cols)}) VALUES ({', '.join(vals)});"


def main():
    with open("ingest_gpu_state.json") as f:
        data = json.load(f)
    
    tracks = data.get("tracks", [])
    pub = [t for t in tracks if t.get("status") == "published" and t.get("uuid")]
    print(f"Total published tracks with UUIDs: {len(pub)}")

    # Generate SQL
    sql_lines = ["BEGIN;"]
    for t in pub:
        sql_lines.append(build_insert(t))
    sql_lines.append("COMMIT;")
    
    sql = "\n".join(sql_lines)
    
    # Write to temp file
    sql_path = "/tmp/zingy_sync.sql"
    with open(sql_path, "w") as f:
        f.write(sql)
    print(f"Generated {len(pub)} INSERT statements → {sql_path}")
    
    if "--dry-run" in sys.argv:
        print("Dry run — not executing. Check /tmp/zingy_sync.sql")
        # Print first 3 statements as preview
        for line in sql_lines[1:4]:
            print(f"  {line[:200]}...")
        return
    
    # Execute on VPS
    print("Executing on VPS...")
    r = subprocess.run(
        f"cat {sql_path} | ssh -o ConnectTimeout=20 {VPS} 'sudo -u postgres psql -d zingy -q'",
        shell=True, capture_output=True, text=True, timeout=120
    )
    if r.returncode == 0:
        print(f"✓ Successfully inserted {len(pub)} tracks into the DB")
    else:
        print(f"✗ Error (exit {r.returncode}):")
        print(r.stderr[-500:] if r.stderr else r.stdout[-500:])

if __name__ == "__main__":
    main()
