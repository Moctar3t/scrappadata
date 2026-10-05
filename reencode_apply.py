import os
import json
import shutil
import subprocess

EXPECT_SCRIPT = "/Users/dime/Developer/scrappadata/fetch_progress.exp"
PROGRESS_FILE_LOCAL = "/Users/dime/Developer/scrappadata/reencode_progress.json"

MP3_DIR = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata-mp3"
M4A_DIR = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata-m4a"

def fetch_progress_remotely():
    print(f"Executing {EXPECT_SCRIPT} to fetch remote progress...")
    try:
        res = subprocess.run([EXPECT_SCRIPT], capture_output=True, text=True, check=True)
        stdout = res.stdout
        
        # Locate the JSON block in stdout
        start_idx = stdout.find("{")
        end_idx = stdout.rfind("}")
        
        if start_idx != -1 and end_idx != -1:
            json_str = stdout[start_idx:end_idx+1]
            # Parse to validate
            data = json.loads(json_str)
            # Save locally
            with open(PROGRESS_FILE_LOCAL, 'w', encoding='utf-8') as out:
                json.dump(data, out, indent=2, ensure_ascii=False)
            print("✓ Successfully fetched and parsed remote progress file.")
            return True
        else:
            print("Error: Could not locate JSON block in expect script output.")
            return False
    except Exception as e:
        print(f"Failed to fetch progress remotely: {e}")
        return False

def main():
    print("=== Re-encode Apply Script ===")
    
    # 1. Fetch remote progress
    if not fetch_progress_remotely():
        if os.path.exists(PROGRESS_FILE_LOCAL):
            print("Falling back to existing local progress file.")
        else:
            print("Error: No progress file available. Cannot proceed.")
            return
            
    with open(PROGRESS_FILE_LOCAL, 'r', encoding='utf-8') as f:
        progress = json.load(f)

    # Count successes
    success_ids = [sid for sid, details in progress.items() if details.get("status") == "success"]
    print(f"Found {len(success_ids)} successfully re-encoded songs in progress file.")
    if not success_ids:
        print("No successful jobs to apply. Exiting.")
        return

    # 2. Reset/Create musicdata-m4a as copy of musicdata-mp3
    if os.path.exists(M4A_DIR):
        print(f"Refreshing {M4A_DIR} from {MP3_DIR}...")
        shutil.rmtree(M4A_DIR)
    else:
        print(f"Creating {M4A_DIR} from {MP3_DIR}...")
    
    try:
        shutil.copytree(MP3_DIR, M4A_DIR)
        print("✓ Copied fallback catalog to musicdata-m4a.")
    except Exception as e:
        print(f"Failed to copy catalog: {e}")
        return

    # 3. Update song JSONs in musicdata-m4a
    updated_count = 0
    songs_dir = os.path.join(M4A_DIR, "songs")
    
    for sid in success_ids:
        song_file = progress[sid].get("filename") or f"{int(sid):04d}.json"
        song_path = os.path.join(songs_dir, song_file)
        
        if os.path.exists(song_path):
            try:
                with open(song_path, 'r', encoding='utf-8') as sf:
                    song_data = json.load(sf)
                
                # Update stems from .mp3 to .m4a
                stems = song_data.get("stems", [])
                modified = False
                for stem in stems:
                    url = stem.get("url", "")
                    if url.endswith(".mp3"):
                        stem["url"] = url[:-4] + ".m4a"
                        modified = True
                
                if modified:
                    with open(song_path, 'w', encoding='utf-8') as sf:
                        json.dump(song_data, sf, indent=2, ensure_ascii=False)
                    updated_count += 1
            except Exception as e:
                print(f"Error updating song {song_file}: {e}")
        else:
            print(f"Warning: Song file {song_file} not found in catalog.")

    print(f"✓ Successfully updated {updated_count} song JSON files in musicdata-m4a.")

if __name__ == "__main__":
    main()
