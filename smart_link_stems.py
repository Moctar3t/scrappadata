import os
import json
import re
import urllib.parse
import subprocess

SONGS_DIR = "/Users/dime/Desktop/AZ-Systems-Hub-2026-04-06/src/data/songs"
STEMS_LIST_PATH = "/Users/dime/Desktop/separated_tracks_list.md"

def robust_normalize(text):
    if not text:
        return ""
    text = text.lower().strip()
    # Strip feat, with, etc.
    text = re.sub(r'\(feat\..*?\)', '', text)
    text = re.sub(r'\(with.*?\)', '', text)
    text = re.sub(r'\[feat\..*?\]', '', text)
    text = re.sub(r'\[with.*?\]', '', text)
    # Strip common suffixes
    text = re.sub(r'\(official audio\)', '', text)
    text = re.sub(r'\(official video\)', '', text)
    text = re.sub(r'\(lyric video\)', '', text)
    text = re.sub(r'\(official video edit\)', '', text)
    text = re.sub(r'\(speed up\)', '', text)
    text = re.sub(r'\(sped up\)', '', text)
    text = re.sub(r'\(slowed\)', '', text)
    text = re.sub(r' - from “.*”', '', text)
    text = re.sub(r' - from ".*"', '', text)
    # Strip all non-alphanumeric
    text = re.sub(r'[^a-z0-9]', '', text)
    return text

def parse_artists(artist_str):
    if not artist_str:
        return []
    # Split by semicolon, comma, ampersand, and the word "and" or "feat"
    parts = re.split(r'[;,/&]| and | feat ', artist_str, flags=re.IGNORECASE)
    return [robust_normalize(p) for p in parts if p.strip()]

def main():
    print("=== Robust Stem Catalog Linker ===")
    
    # 1. Load separated folders from separated_tracks_list.md
    separated_folders = []
    if not os.path.exists(STEMS_LIST_PATH):
        print(f"Error: Stems list {STEMS_LIST_PATH} not found.")
        return
        
    with open(STEMS_LIST_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            m = re.search(r'`([^`]+)`', line)
            if m:
                folder_name = m.group(1).strip()
                parts = folder_name.split(' - ', 1)
                if len(parts) == 2:
                    art_part, title_part = parts[0].strip(), parts[1].strip()
                    separated_folders.append({
                        "folder": folder_name,
                        "raw_artist": art_part,
                        "raw_title": title_part,
                        "norm_artist": robust_normalize(art_part),
                        "norm_title": robust_normalize(title_part),
                        "split_artists": parse_artists(art_part)
                    })
    print(f"Loaded {len(separated_folders)} separated folders from {STEMS_LIST_PATH}.")

    # 2. Load song JSON files
    song_files = []
    if not os.path.exists(SONGS_DIR):
        print(f"Error: Songs directory {SONGS_DIR} does not exist.")
        return
        
    for filename in os.listdir(SONGS_DIR):
        if filename.endswith(".json"):
            filepath = os.path.join(SONGS_DIR, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    song = json.load(f)
                    song_files.append((filename, filepath, song))
            except Exception as e:
                print(f"Failed to read {filename}: {e}")
    print(f"Loaded {len(song_files)} song files from {SONGS_DIR}.")

    # 3. Match and Update
    matched_count = 0
    updated_count = 0
    
    for filename, filepath, song in song_files:
        song_title_norm = robust_normalize(song.get('title', ''))
        song_artists = parse_artists(song.get('artist', ''))
        
        match_folder = None
        
        # Heuristic 1: Perfect match of artist & title normalized
        for sf in separated_folders:
            if sf['norm_title'] == song_title_norm:
                # Check if artists overlap or match
                artist_match = False
                if sf['norm_artist'] == robust_normalize(song.get('artist', '')):
                    artist_match = True
                else:
                    # Check for overlap in split artists
                    for sa in song_artists:
                        if sa in sf['split_artists'] or any(sa in s or s in sa for s in sf['split_artists']):
                            artist_match = True
                            break
                if artist_match:
                    match_folder = sf
                    break
                    
        # Heuristic 2: Substring or looser title match if artist has strong overlap
        if not match_folder:
            for sf in separated_folders:
                # Title starts with or is contained in
                title_match = False
                if sf['norm_title'] == song_title_norm or sf['norm_title'] in song_title_norm or song_title_norm in sf['norm_title']:
                    title_match = True
                
                if title_match:
                    # Require at least one artist to match/overlap
                    for sa in song_artists:
                        if sa in sf['split_artists'] or any(sa in s or s in sa for s in sf['split_artists']):
                            match_folder = sf
                            break
                if match_folder:
                    break
                    
        if match_folder:
            matched_count += 1
            # Check if stems needs updating or is currently empty
            existing_stems = song.get('stems', [])
            
            # We want to make sure it has the correct CDN URLs
            quoted_folder = urllib.parse.quote(match_folder['folder'])
            new_stems = [
                {"id": "bass", "name": "BASS", "url": f"https://dimesystemsstorage.b-cdn.net/{quoted_folder}/bass.m4a"},
                {"id": "drums", "name": "DRUMS", "url": f"https://dimesystemsstorage.b-cdn.net/{quoted_folder}/drums.m4a"},
                {"id": "other", "name": "OTHER", "url": f"https://dimesystemsstorage.b-cdn.net/{quoted_folder}/other.m4a"},
                {"id": "vocals", "name": "VOCALS", "url": f"https://dimesystemsstorage.b-cdn.net/{quoted_folder}/vocals.m4a"}
            ]
            
            # Only write if stems array is empty or different
            if not existing_stems or len(existing_stems) != 4 or existing_stems[0].get('url') != new_stems[0]['url']:
                song['stems'] = new_stems
                try:
                    with open(filepath, 'w', encoding='utf-8') as f:
                        json.dump(song, f, indent=2)
                    updated_count += 1
                except Exception as e:
                    print(f"Failed to write updated song {filename}: {e}")

    print(f"\nMatched {matched_count} / {len(separated_folders)} separated folders to song files.")
    print(f"Updated stems arrays in {updated_count} song JSON files.")
    
    # 4. Compile public index
    print("\nRunning build-music-catalog script to compile the new index...")
    try:
        res = subprocess.run(["node", "scripts/build-music-catalog.mjs"], capture_output=True, text=True, check=True)
        print(res.stdout)
    except Exception as e:
        print(f"Failed to compile catalog index: {e}")

if __name__ == "__main__":
    main()
