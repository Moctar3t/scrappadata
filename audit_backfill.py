import json
import subprocess

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

def main():
    db_tracks = get_db_tracks()
    print(f"\nTotal DB tracks: {len(db_tracks)}")
    
    # 1. Sample original_url formats
    print("\nSample original_url formats:")
    for t in db_tracks[:5]:
        print(f" - {t.get('original_url')}")
    
    # Check if there are any non-standard formats
    non_standard = [t for t in db_tracks if not str(t.get('original_url', '')).startswith("https://www.youtube.com/watch?v=")]
    print(f"\nNon-standard original_url count: {len(non_standard)}")
    if non_standard:
        print("Examples:")
        for t in non_standard[:3]:
            print(f" - {t.get('original_url')}")

    # 2. Match against youtube_search_cache
    try:
        with open("youtube_search_cache.json", "r") as f:
            yt_cache = json.load(f)
    except Exception as e:
        print(f"Failed to load youtube cache: {e}")
        yt_cache = {}

    # Extract all cached vids
    cached_vids = set()
    for hits in yt_cache.values():
        for hit in hits:
            if "vid" in hit:
                cached_vids.add(hit["vid"])
                
    # 3. Dry run stats
    matched = 0
    missing = 0
    ambiguous = 0
    already_complete = 0
    needs_update = 0

    try:
        with open("genius_enrich_cache.json", "r") as f:
            genius_cache = json.load(f)
    except:
        genius_cache = {}

    for t in db_tracks:
        url = t.get("original_url", "")
        writers = t.get("writers")
        
        artist = t.get("artist") or ""
        primary = artist.split(',')[0].split('&')[0].split(' x ')[0].split(' X ')[0].strip()
        q = f"{primary} {t.get('title', '')}"
        
        if q in genius_cache:
            matched += 1
            if writers:
                already_complete += 1
            elif genius_cache[q].get("writers"):
                needs_update += 1
        else:
            missing += 1

    print("\n--- Dry Run Stats ---")
    print(f"Matched: {matched}")
    print(f"Missing database row (or no cache hit): {missing}")
    print(f"Ambiguous matches: {ambiguous}")
    print(f"Already complete: {already_complete}")
    print(f"Needs update (has local writers): {needs_update}")

if __name__ == "__main__":
    main()
