import json
import csv
import os
import re

csv_path = "/Users/dime/Downloads/Liked_Songs.csv"
music_index_path = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata/music-index.json"
songs_dir = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata/songs"

def normalize(text):
    return re.sub(r'[^a-z0-9]', '', str(text).lower())

def write_json_if_changed(filepath, data, **kwargs):
    """Only touch the file when the serialized content actually differs — the
    same guard export_catalog.py uses. Without it every run rewrites every
    matched song regardless of whether anything changed, and a formatting
    mismatch (see indent below) turns that into a full-file diff each time."""
    new_content = json.dumps(data, **kwargs)
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            if f.read() == new_content:
                return False
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(new_content)
    return True

def process():
    if not os.path.exists(csv_path):
        print("CSV not found")
        return
    
    # Load CSV
    metrics = {}
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = normalize(row["Track Name"])
            artist = normalize(row["Artist Name(s)"].split(',')[0]) # Primary artist
            key = f"{title}_{artist}"
            metrics[key] = row
            # Fallback map for title only
            metrics[title] = row

    if not os.path.exists(music_index_path):
        print("music-index.json not found")
        return
        
    with open(music_index_path, "r", encoding="utf-8") as f:
        catalog = json.load(f)
        
    matched = 0
    for track in catalog.get("songsList", []):
        title = normalize(track.get("title", ""))
        artist = normalize(track.get("artist", "").split(';')[0])
        key = f"{title}_{artist}"
        
        row = metrics.get(key) or metrics.get(title)
        
        if row:
            matched += 1
            # Genres
            genres_raw = row.get("Genres", "")
            track["genres"] = [g.strip() for g in genres_raw.split(",")] if genres_raw else []
            
            # Float metrics
            for field in ["Danceability", "Energy", "Acousticness", "Instrumentalness", "Valence", "Speechiness"]:
                val = row.get(field, "")
                track[field.lower()] = float(val) if val else 0.0
                
            # Int metrics
            pop = row.get("Popularity", "")
            track["popularity"] = int(pop) if pop else 0
            
            # Save into the individual song file. Ids under 1000 are stored
            # zero-padded to 4 digits on disk (0002.json, not 2.json) — using
            # the raw id here made `os.path.exists` false for every song below
            # id 1000, so their per-song file silently never got the CSV
            # enrichment (only the index did).
            song_id = track.get("id")
            if song_id:
                padded_id = "%04d" % int(song_id)
                song_path = os.path.join(songs_dir, f"{padded_id}.json")
                if os.path.exists(song_path):
                    with open(song_path, "r", encoding="utf-8") as sf:
                        song_data = json.load(sf)

                    song_data["genres"] = track["genres"]
                    for field in ["danceability", "energy", "acousticness", "instrumentalness", "valence", "speechiness", "popularity"]:
                        song_data[field] = track[field]

                    # indent=2 matches every other pipeline script (export_catalog.py,
                    # refine_genres.py, enrich_producers.py, reconcile_metadata.py).
                    # Writing minified here used to flip each touched file to one
                    # line; the next script run then flipped it back to pretty —
                    # a full-file reformat ping-pong with zero real content change,
                    # which is what balloons a small CSV re-run into a +14K LOC diff.
                    write_json_if_changed(song_path, song_data, indent=2, ensure_ascii=False)

    write_json_if_changed(music_index_path, catalog, separators=(',', ':'), ensure_ascii=False)

    print(f"Matched and enriched {matched}/{len(catalog)} tracks!")

if __name__ == '__main__':
    process()
