#!/usr/bin/env python3
import json
import os
import re
import uuid
import subprocess

VPS_HOST = "204.168.236.140"
APP_DIR = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
INDEX_PATH = os.path.join(APP_DIR, "music-index.json")
SONGS_DIR = os.path.join(APP_DIR, "songs")
ID_MAP_PATH = "/Users/dime/Developer/scrappadata/catalog_id_map.json"
CATALOG_USER_ID = "30b305c6-5b19-43de-acc3-9ba2fa71a565"

def esc(v):
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"

def main():
    print("=== Migrating Legacy Tracks to VPS Postgres Database ===")

    # 1. Load music-index.json
    if not os.path.exists(INDEX_PATH):
        print(f"Error: music-index.json not found at {INDEX_PATH}")
        return
    with open(INDEX_PATH, 'r', encoding='utf-8') as f:
        catalog = json.load(f)

    # 2. Load catalog_id_map.json
    id_map = {}
    if os.path.exists(ID_MAP_PATH):
        try:
            id_map = json.load(open(ID_MAP_PATH, encoding="utf-8"))
        except Exception:
            id_map = {}

    mapped_ids = set(id_map.values())
    songs_list = catalog.get("songsList", [])
    print(f"Total songs in index: {len(songs_list)}")
    print(f"Total mapped from DB: {len(mapped_ids)}")

    unmapped = [s for s in songs_list if str(s["id"]) not in mapped_ids]
    print(f"Found {len(unmapped)} unmapped legacy tracks to migrate.")

    if not unmapped:
        print("No legacy tracks to migrate.")
        return

    # 3. Generate SQL statements
    sqls = []
    new_mappings = {}

    for idx, s in enumerate(unmapped, 1):
        num_id = str(s["id"])
        padded_id = num_id.zfill(4)
        song_json_path = os.path.join(SONGS_DIR, f"{padded_id}.json")

        # Load detail file if it exists, otherwise use index values
        detail = {}
        if os.path.exists(song_json_path):
            try:
                detail = json.load(open(song_json_path, encoding="utf-8"))
            except Exception:
                pass

        # Merge fields
        title = detail.get("title") or s.get("title") or ""
        artist = detail.get("artist") or s.get("artist") or ""
        producers = detail.get("producers") or s.get("producers") or ""
        writers = detail.get("writers") or s.get("writers") or ""
        record_label = detail.get("recordLabel") or s.get("recordLabel") or ""
        bpm = detail.get("bpm") or s.get("bpm") or 0
        song_key = detail.get("key") or s.get("key") or ""
        duration_sec = detail.get("duration") or 0
        if not duration_sec and s.get("duration"):
            # Parse display duration (e.g. "3:22")
            parts = s["duration"].split(":")
            if len(parts) == 2:
                duration_sec = int(parts[0]) * 60 + int(parts[1])
        
        cover_url = detail.get("coverArt") or s.get("coverArt") or ""
        cover_url_fallback = detail.get("coverArtFallback") or s.get("coverArtFallback") or ""

        # Extract stems
        stems = detail.get("stems", [])
        stem_urls = {"bass": None, "drums": None, "vocals": None, "other": None}
        for stem in stems:
            stem_id = stem.get("id")
            if stem_id in stem_urls:
                stem_urls[stem_id] = stem.get("url")

        # Parse year
        year_str = str(detail.get("year") or s.get("year") or "").strip()
        year_val = "NULL"
        if year_str.isdigit():
            year_val = int(year_str)

        # Generate new UUID
        track_uuid = str(uuid.uuid4())
        new_mappings[track_uuid] = num_id

        # Insert SQL statement
        sql = (
            f"INSERT INTO tracks ("
            f"id, owner_id, title, artist, producers, writers, year, record_label, bpm, song_key, "
            f"duration_sec, cover_url, cover_url_fallback, bass_url, drums_url, vocals_url, other_url, "
            f"visibility, status, split_source, attempts, created_at, ready_at, meta_analyzed"
            f") VALUES ("
            f"'{track_uuid}', '{CATALOG_USER_ID}', {esc(title)}, {esc(artist)}, {esc(producers)}, {esc(writers)}, "
            f"{year_val}, {esc(record_label)}, {bpm}, {esc(song_key)}, {duration_sec}, "
            f"{esc(cover_url)}, {esc(cover_url_fallback)}, {esc(stem_urls['bass'])}, {esc(stem_urls['drums'])}, "
            f"{esc(stem_urls['vocals'])}, {esc(stem_urls['other'])}, 'public', 'ready', 'provided', 1, "
            f"now(), now(), true"
            f");"
        )
        sqls.append(sql)

    # 4. Write SQL file locally
    sql_file = "/tmp/migrate_legacy.sql"
    with open(sql_file, "w", encoding="utf-8") as f:
        f.write("\n".join(sqls) + "\n")

    print(f"Generated SQL file with {len(sqls)} insertions at {sql_file}")

    # 5. Execute SQL file on VPS via ssh
    print("Uploading and executing SQL on VPS Postgres database...")
    ssh_cmd = ["ssh", f"root@{VPS_HOST}", "sudo -u postgres psql -d zingy"]
    with open(sql_file, "rb") as f_in:
        r = subprocess.run(ssh_cmd, stdin=f_in, capture_output=True, text=True)

    if r.returncode != 0:
        print(f"Postgres execution failed: {r.stderr}")
        return
    else:
        print("Postgres execution successful!")

    # 6. Save new mappings back to catalog_id_map.json
    id_map.update(new_mappings)
    with open(ID_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(id_map, f, indent=0, ensure_ascii=False)
    print(f"Updated {ID_MAP_PATH} with {len(new_mappings)} new mappings.")

if __name__ == "__main__":
    main()
