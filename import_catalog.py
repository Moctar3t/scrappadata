import os
import json
import re
import urllib.request
import urllib.parse
import time

# Paths
SONGS_DIR = "/Users/dime/Desktop/AZ-Systems-Hub-2026-04-06/src/data/songs"
AUDIT_PATH = "/Users/dime/Desktop/liked_songs_metadata_audit.md"
STEMS_LIST_PATH = "/Users/dime/Desktop/separated_tracks_list.md"
CARDS_PATH = "/Users/dime/Developer/scrappadata/metadata_cards.json"

def normalize(text):
    if not text:
        return ""
    text = text.lower().strip()
    # Remove subtitles or features for robust matching
    text = re.sub(r' - from “.*”', '', text)
    text = re.sub(r' - from ".*"', '', text)
    text = re.sub(r'\(feat\..*?\)', '', text)
    text = re.sub(r'\(with.*?\)', '', text)
    text = re.sub(r'[^a-z0-9]', '', text)
    return text

def parse_duration(dur_str):
    try:
        parts = list(map(int, dur_str.strip().split(':')))
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        elif len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except Exception:
        pass
    return 240

def fetch_itunes_cover(artist, title):
    query = f"{artist} {title}"
    url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&entity=song&limit=1"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=2.0) as response:
            data = json.loads(response.read().decode())
            if data['resultCount'] > 0:
                result = data['results'][0]
                art = result.get('artworkUrl100', '').replace('100x100bb', '1000x1000bb')
                return art
    except Exception as e:
        # Silently fail, using empty string
        pass
    return ""

def main():
    print("=== Dimé Music Catalog Importer ===")
    
    # 1. Load existing songs to prevent overwriting
    existing_songs = {}
    if os.path.exists(SONGS_DIR):
        for filename in os.listdir(SONGS_DIR):
            if filename.endswith(".json"):
                filepath = os.path.join(SONGS_DIR, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        song = json.load(f)
                        norm_key = normalize(song['artist']) + " - " + normalize(song['title'])
                        existing_songs[norm_key] = song
                except Exception as e:
                    print(f"Failed to read existing file {filename}: {e}")
    print(f"Loaded {len(existing_songs)} existing tracks from {SONGS_DIR}")

    # 2. Load cover art lookup from metadata_cards.json
    cover_lookup = {}
    if os.path.exists(CARDS_PATH):
        try:
            with open(CARDS_PATH, 'r', encoding='utf-8') as f:
                cards = json.load(f)
                for c in cards:
                    key = normalize(c['artist']) + " - " + normalize(c['title'])
                    cover_lookup[key] = c.get('coverArt', '')
            print(f"Loaded {len(cover_lookup)} cover art references from metadata_cards.json")
        except Exception as e:
            print(f"Failed to load cover art references: {e}")

    # 3. Parse separated stems list
    separated_folders = {}
    if os.path.exists(STEMS_LIST_PATH):
        with open(STEMS_LIST_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                m = re.search(r'`([^`]+)`', line)
                if m:
                    folder_name = m.group(1).strip()
                    parts = folder_name.split(' - ', 1)
                    if len(parts) == 2:
                        art_name, trk_name = parts[0].strip(), parts[1].strip()
                        key = normalize(art_name) + " - " + normalize(trk_name)
                        separated_folders[key] = folder_name
        print(f"Parsed {len(separated_folders)} completed stem folders from separated_tracks_list.md")
    else:
        print(f"Warning: {STEMS_LIST_PATH} not found!")

    # 4. Parse liked songs metadata audit
    audit_tracks = []
    if os.path.exists(AUDIT_PATH):
        with open(AUDIT_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip().startswith('|') and not line.strip().startswith('|---'):
                    parts = [p.strip() for p in line.split('|')]
                    # Table row: | index | Track Name | Artist Name | Verified Producers | Release Date | Key | BPM | Duration | Label |
                    if len(parts) >= 11:
                        idx_str = parts[1]
                        if idx_str.isdigit():
                            title = parts[2].replace('**', '').strip()
                            artist = parts[3].strip()
                            producers = parts[4].replace('*', '').strip()
                            release_date = parts[5].strip()
                            key = parts[6].strip()
                            bpm_str = parts[7].strip()
                            duration_str = parts[8].strip()
                            record_label = parts[9].strip()
                            
                            bpm = 120
                            try:
                                bpm = int(bpm_str)
                            except ValueError:
                                pass
                            
                            audit_tracks.append({
                                "title": title,
                                "artist": artist,
                                "producers": producers,
                                "releaseDate": release_date,
                                "key": key,
                                "bpm": bpm,
                                "duration_str": duration_str,
                                "recordLabel": record_label
                            })
        print(f"Parsed {len(audit_tracks)} tracks from liked_songs_metadata_audit.md")
    else:
        print(f"Error: {AUDIT_PATH} not found!")
        return

    # Determine starting ID for new tracks
    existing_ids = []
    for song in existing_songs.values():
        try:
            existing_ids.append(int(song['id']))
        except ValueError:
            pass
    next_id = max(existing_ids) + 1 if existing_ids else 81
    print(f"New tracks will be assigned IDs starting from {next_id}")

    # 5. Process & Generate JSONs
    new_tracks_count = 0
    updated_stems_count = 0
    itunes_fetches = 0
    
    for item in audit_tracks:
        norm_key = normalize(item['artist']) + " - " + normalize(item['title'])
        
        # Check if already exists in existing songs
        if norm_key in existing_songs:
            existing_song = existing_songs[norm_key]
            # Check if this existing song now has stems available, but stems array is empty
            if not existing_song.get('stems') and norm_key in separated_folders:
                folder_name = separated_folders[norm_key]
                quoted = urllib.parse.quote(folder_name)
                existing_song['stems'] = [
                    {"id": "bass", "name": "BASS", "url": f"https://dimesystemsstorage.b-cdn.net/{quoted}/bass.mp3"},
                    {"id": "drums", "name": "DRUMS", "url": f"https://dimesystemsstorage.b-cdn.net/{quoted}/drums.mp3"},
                    {"id": "other", "name": "OTHER", "url": f"https://dimesystemsstorage.b-cdn.net/{quoted}/other.mp3"},
                    {"id": "vocals", "name": "VOCALS", "url": f"https://dimesystemsstorage.b-cdn.net/{quoted}/vocals.mp3"}
                ]
                padded = str(existing_song['id']).zfill(4)
                filepath = os.path.join(SONGS_DIR, f"{padded}.json")
                with open(filepath, 'w', encoding='utf-8') as out_f:
                    json.dump(existing_song, out_f, indent=2)
                updated_stems_count += 1
            continue
            
        # New song to import!
        # Resolve Cover Art
        cover_art = cover_lookup.get(norm_key, "")
        if not cover_art:
            # Query iTunes search API, but throttle it to be good citizens
            if itunes_fetches < 150: # Limit to first 150 missing covers to prevent huge lag, rest can use placeholder
                cover_art = fetch_itunes_cover(item['artist'], item['title'])
                itunes_fetches += 1
                time.sleep(0.1) # 100ms throttle
            if not cover_art:
                cover_art = "" # Use empty string or default UI placeholder

        # Resolve Stems
        folder_name = separated_folders.get(norm_key)
        if folder_name:
            quoted = urllib.parse.quote(folder_name)
            stems = [
                {"id": "bass", "name": "BASS", "url": f"https://dimesystemsstorage.b-cdn.net/{quoted}/bass.mp3"},
                {"id": "drums", "name": "DRUMS", "url": f"https://dimesystemsstorage.b-cdn.net/{quoted}/drums.mp3"},
                {"id": "other", "name": "OTHER", "url": f"https://dimesystemsstorage.b-cdn.net/{quoted}/other.mp3"},
                {"id": "vocals", "name": "VOCALS", "url": f"https://dimesystemsstorage.b-cdn.net/{quoted}/vocals.mp3"}
            ]
        else:
            stems = []

        duration_sec = parse_duration(item['duration_str'])
        
        song_id = str(next_id)
        padded_id = song_id.zfill(4)
        
        merged_song = {
            "id": song_id,
            "title": item['title'],
            "artist": item['artist'],
            "coverArt": cover_art,
            "displayDuration": item['duration_str'],
            "duration": duration_sec,
            "bpm": item['bpm'],
            "key": item['key'],
            "year": item['releaseDate'][:4] if item['releaseDate'] else "Unknown",
            "producers": item['producers'],
            "releaseDate": item['releaseDate'],
            "recordLabel": item['recordLabel'],
            "stems": stems
        }
        
        filepath = os.path.join(SONGS_DIR, f"{padded_id}.json")
        with open(filepath, 'w', encoding='utf-8') as out_f:
            json.dump(merged_song, out_f, indent=2)
            
        next_id += 1
        new_tracks_count += 1

    print(f"Successfully processed and updated {updated_stems_count} existing tracks with stems.")
    print(f"Successfully generated {new_tracks_count} new song JSON files under {SONGS_DIR}.")
    print("Catalog integration complete!")

if __name__ == "__main__":
    main()
