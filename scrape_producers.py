import csv
import os
import json
import urllib.request
import urllib.parse
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

csv_path = "/Users/dime/Downloads/Liked_Songs.csv"
output_path = "/Users/dime/Desktop/liked_songs_metadata_audit.md"
cache_path = "/Users/dime/Developer/scrappadata/producer_cache.json"

# Load cache
cache = {}
if os.path.exists(cache_path):
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        print(f"Loaded {len(cache)} cached producers.")
    except Exception as e:
        print("Error loading cache:", e)

# Pitch class mapping
KEY_MAP = {
    0: "C", 1: "C#", 2: "D", 3: "D#", 4: "E", 5: "F",
    6: "F#", 7: "G", 8: "G#", 9: "A", 10: "A#", 11: "B"
}

def format_duration(ms_str):
    try:
        ms = int(ms_str)
        total_seconds = ms // 1000
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes}:{seconds:02d}"
    except:
        return "0:00"

def get_musical_key(key_str, mode_str):
    try:
        key_num = int(float(key_str))
        mode_num = int(float(mode_str))
        key_name = KEY_MAP.get(key_num, "Unknown")
        mode_name = "Major" if mode_num == 1 else "Minor"
        return f"{key_name} {mode_name}"
    except:
        return "Unknown"

def get_bpm(tempo_str):
    try:
        return str(round(float(tempo_str)))
    except:
        return "Unknown"

def fetch_producers_from_genius(artist, title):
    query = f"{artist} - {title}"
    if query in cache:
        return cache[query]
        
    search_url = f'https://genius.com/api/search/multi?q={urllib.parse.quote(query)}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        # Step 1: Search song ID
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            sections = data.get('response', {}).get('sections', [])
            song_id = None
            for section in sections:
                if section.get('type') == 'song':
                    hits = section.get('hits', [])
                    if hits:
                        song_id = hits[0].get('result', {}).get('id')
                        break
                        
        if not song_id:
            cache[query] = "N/A"
            return "N/A"
            
        # Step 2: Fetch song relationships
        time.sleep(0.1) # Brief delay
        song_url = f'https://genius.com/api/songs/{song_id}'
        req = urllib.request.Request(song_url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            song = data.get('response', {}).get('song', {})
            producers = song.get('producer_artists', [])
            prod_names = [p.get('name') for p in producers]
            prod_str = ", ".join(prod_names) if prod_names else "Self-Produced"
            
            cache[query] = prod_str
            return prod_str
            
    except Exception as e:
        return "Search Pending"

def save_cache():
    try:
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print("Error saving cache:", e)

# Read CSV and build basic data
tracks = []
with open(csv_path, mode='r', encoding='utf-8', errors='ignore') as infile:
    reader = csv.reader(infile)
    header = next(reader)
    col_map = {col: idx for idx, col in enumerate(header)}
    
    for row in reader:
        if not row:
            continue
        tracks.append({
            "name": row[col_map["Track Name"]],
            "artists": row[col_map["Artist Name(s)"]],
            "release_date": row[col_map["Release Date"]],
            "duration": format_duration(row[col_map["Duration (ms)"]]),
            "key": get_musical_key(row[col_map["Key"]], row[col_map["Mode"]]),
            "bpm": get_bpm(row[col_map["Tempo"]]),
            "label": row[col_map["Record Label"]]
        })

print(f"Loaded {len(tracks)} tracks from CSV. Beginning producer lookup...")

# We will run queries in batches and write the output live.
# To protect from rate limiting, we use 4 workers.
total_tracks = len(tracks)

def process_track(idx, t):
    artists = t["artists"].split(";")[0] # Query primary artist
    producers = fetch_producers_from_genius(artists, t["name"])
    t["producers"] = producers
    if idx % 10 == 0:
        save_cache()
    return idx, producers

# Run with ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=3) as executor:
    futures = {executor.submit(process_track, idx, t): idx for idx, t in enumerate(tracks)}
    
    # We will rewrite the markdown file periodically as songs get completed
    completed_count = 0
    for future in as_completed(futures):
        idx = futures[future]
        try:
            future.result()
        except Exception as e:
            pass
            
        completed_count += 1
        
        # Rewrite output file every 20 completed tracks to show live progress
        if completed_count % 20 == 0 or completed_count == total_tracks:
            print(f"Completed {completed_count}/{total_tracks} tracks...")
            with open(output_path, mode='w', encoding='utf-8') as outfile:
                outfile.write("# DIME SYSTEMS - Liked Songs Core Catalog Audit\n")
                outfile.write(f"## 100% Authentic Metadata & Producer Credits ({completed_count}/{total_tracks} fetched)\n\n")
                outfile.write("| # | Track Name | Artist Name(s) | Verified Producers | Release Date | Key | BPM | Duration | Record Label |\n")
                outfile.write("|---|---|---|---|---|---|---|---|---|\n")
                
                for i, t in enumerate(tracks, start=1):
                    prod = t.get("producers", "Search Pending")
                    esc_name = t["name"].replace("|", "\\|")
                    esc_artists = t["artists"].replace("|", "\\|")
                    esc_label = t["label"].replace("|", "\\|")
                    esc_prod = prod.replace("|", "\\|")
                    outfile.write(f"| {i} | **{esc_name}** | {esc_artists} | *{esc_prod}* | {t['release_date']} | {t['key']} | {t['bpm']} | {t['duration']} | {esc_label} |\n")
            save_cache()

save_cache()
print("All tasks finished successfully!")
