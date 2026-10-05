import json
import subprocess
import argparse
from datetime import datetime

VPS_HOST = "204.168.236.140"

def get_db_tracks():
    print("Fetching tracks from VPS database...")
    query = "SELECT row_to_json(t) FROM tracks t WHERE status='ready' AND visibility='public' AND owner_id=(SELECT id FROM users WHERE username='__catalog__')"
    cmd = ["ssh", f"root@{VPS_HOST}", f"sudo -u postgres psql -d zingy -t -A -c \"{query}\""]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    tracks = []
    for line in res.stdout.strip().split('\n'):
        if line:
            tracks.append(json.loads(line))
    return tracks

def escape_sql(val):
    if not val:
        return "NULL"
    # double single quotes for postgres string escaping
    escaped = str(val).replace("'", "''")
    return f"'{escaped}'"

def main():
    parser = argparse.ArgumentParser(description="Backfill missing metadata to database")
    parser.add_argument("--dry-run", action="store_true", help="Print stats without modifying the database")
    args = parser.parse_args()

    db_tracks = get_db_tracks()
    
    try:
        with open("genius_enrich_cache.json", "r") as f:
            genius_cache = json.load(f)
    except:
        genius_cache = {}

    matched = 0
    missing = 0
    already_complete = 0
    needs_update = 0

    updates = []
    log_entries = []

    for t in db_tracks:
        url = t.get("original_url", "")
        if not url.startswith("https://www.youtube.com/watch?v="):
            continue

        artist = t.get("artist") or ""
        primary = artist.split(',')[0].split('&')[0].split(' x ')[0].split(' X ')[0].strip()
        q = f"{primary} {t.get('title', '')}"
        
        if q not in genius_cache:
            missing += 1
            continue

        matched += 1
        d = genius_cache[q]
        
        # Check what fields we have in cache that are missing in DB
        db_producers = t.get("producers")
        db_writers = t.get("writers")
        db_featuring = t.get("featuring")

        c_producers = d.get("producers", [])
        c_writers = d.get("writers", [])
        c_featuring = d.get("featuring", [])

        c_producers_str = "; ".join(c_producers) if c_producers else None
        c_writers_str = "; ".join(c_writers) if c_writers else None
        c_featuring_str = "; ".join(c_featuring) if c_featuring else None

        fields_to_update = {}
        if not db_producers and c_producers_str:
            fields_to_update["producers"] = c_producers_str
        if not db_writers and c_writers_str:
            fields_to_update["writers"] = c_writers_str
        if not db_featuring and c_featuring_str:
            fields_to_update["featuring"] = c_featuring_str

        if not fields_to_update:
            already_complete += 1
            continue

        needs_update += 1
        
        # Build SQL Update using COALESCE
        set_clauses = []
        for col, val in fields_to_update.items():
            set_clauses.append(f"{col} = COALESCE({col}, {escape_sql(val)})")
        
        sql = f"UPDATE tracks SET {', '.join(set_clauses)} WHERE original_url = {escape_sql(url)};"
        updates.append(sql)

        # Build log entry
        timestamp = datetime.utcnow().isoformat()
        fields_str = ", ".join(fields_to_update.keys())
        log_entries.append(f"{t['id']} | {url} | {fields_str} | {timestamp}")

    print("\n--- Dry Run Stats ---")
    print(f"Matched in cache: {matched}")
    print(f"Missing from cache: {missing}")
    print(f"Already complete: {already_complete}")
    print(f"Needs update: {needs_update}")

    if args.dry_run:
        print("\n[DRY RUN] No data modified.")
        if updates:
            print("\nSample SQL Update:")
            print(updates[0])
            print("\nSample Migration Log:")
            print(log_entries[0])
        return

    if not updates:
        print("\nNo updates required.")
        return

    print(f"\nApplying {len(updates)} updates to database...")
    sql_script = "BEGIN;\n" + "\n".join(updates) + "\nCOMMIT;\n"
    
    with open("backfill_batch.sql", "w") as f:
        f.write(sql_script)
    
    cmd = ["ssh", f"root@{VPS_HOST}", "sudo -u postgres psql -d zingy -f -"]
    try:
        subprocess.run(cmd, input=sql_script, text=True, check=True)
        print("Database updated successfully.")
        
        with open("migration.log", "a") as f:
            for log in log_entries:
                f.write(log + "\n")
        print("Appended migration records to migration.log")
        
    except subprocess.CalledProcessError as e:
        print(f"Database update failed: {e}")

if __name__ == "__main__":
    main()
