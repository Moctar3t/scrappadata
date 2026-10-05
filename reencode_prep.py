import os
import json
import shutil
import urllib.parse

SONGS_DIR = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata/songs"
MUSICDATA_DIR = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
BACKUP_DIR = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata-mp3"
OUTPUT_JOBS_PATH = "/Users/dime/Developer/scrappadata/reencode_jobs.json"

def main():
    print("=== Re-encode Pipeline Prep Script ===")
    
    # 1. Back up active catalog to musicdata-mp3
    if os.path.exists(BACKUP_DIR):
        print(f"Backup catalog already exists at {BACKUP_DIR}. Skipping copy.")
    else:
        print(f"Creating catalog backup at {BACKUP_DIR}...")
        try:
            shutil.copytree(MUSICDATA_DIR, BACKUP_DIR)
            print("✓ Catalog backup created successfully.")
        except Exception as e:
            print(f"Failed to create catalog backup: {e}")
            return

    # 2. Scan songs and build job list
    if not os.path.exists(SONGS_DIR):
        print(f"Error: Songs folder {SONGS_DIR} does not exist.")
        return

    jobs = []
    song_files = [f for f in os.listdir(SONGS_DIR) if f.endswith(".json")]
    
    print(f"Scanning {len(song_files)} song JSON files...")
    
    for f in song_files:
        filepath = os.path.join(SONGS_DIR, f)
        try:
            with open(filepath, 'r', encoding='utf-8') as file:
                song = json.load(file)
            
            stems = song.get("stems", [])
            if len(stems) == 4:
                # Extract folder name from URL of the first stem
                url = stems[0]["url"] # e.g. https://dimesystemsstorage.b-cdn.net/Folder/bass.mp3
                # Split by host and extract the parent directory of bass.mp3
                path_part = url.split("dimesystemsstorage.b-cdn.net/")[-1]
                folder_quoted = os.path.dirname(path_part)
                folder_name = urllib.parse.unquote(folder_quoted)
                
                if folder_name:
                    jobs.append({
                        "id": song["id"],
                        "title": song.get("title", "Unknown"),
                        "artist": song.get("artist", "Unknown"),
                        "folder": folder_name,
                        "filename": f
                    })
        except Exception as e:
            print(f"Error parsing {f}: {e}")

    print(f"Found {len(jobs)} songs with stems.")
    
    # 3. Save job list
    with open(OUTPUT_JOBS_PATH, 'w', encoding='utf-8') as out:
        json.dump(jobs, out, indent=2, ensure_ascii=False)
    print(f"✓ Saved {len(jobs)} jobs to {OUTPUT_JOBS_PATH}")

if __name__ == "__main__":
    main()
