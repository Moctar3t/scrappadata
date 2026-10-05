import os
import json
import sys

# Ensure UTF-8 output encoding if possible
try:
    sys.stdout.reconfigure(encoding='utf-8')
except:
    pass

tracks_dir = "tracks"
json_path = "files_to_delete.json"

if not os.path.exists(json_path):
    print("Error: files_to_delete.json not found!")
    exit(1)

with open(json_path, "r", encoding="utf-8") as f:
    files = json.load(f)

print(f"Loaded {len(files)} files to delete from list.")

deleted_count = 0
for filename in files:
    target_path = os.path.join(tracks_dir, filename)
    if os.path.exists(target_path):
        try:
            os.remove(target_path)
            try:
                print(f"Deleted: {filename}")
            except:
                print("Deleted a track (non-ASCII name)")
            deleted_count += 1
        except Exception as e:
            print(f"Error deleting file: {e}")
    else:
        # Try a quick fuzzy match in case of minor naming variations
        found_fuzzy = False
        if os.path.exists(tracks_dir):
            for existing in os.listdir(tracks_dir):
                # Compare without extension and case insensitively
                if existing.lower().replace(" ", "") == filename.lower().replace(" ", ""):
                    try:
                        os.remove(os.path.join(tracks_dir, existing))
                        try:
                            print(f"Fuzzy Deleted: {existing}")
                        except:
                            print("Fuzzy Deleted a track (non-ASCII name)")
                        deleted_count += 1
                        found_fuzzy = True
                        break
                    except Exception as e:
                        print(f"Error fuzzy deleting: {e}")
        if not found_fuzzy:
            try:
                print(f"Not found on disk: {filename}")
            except:
                print("Track not found on disk (non-ASCII name)")

print(f"Cleanup finished. Successfully deleted {deleted_count} files.")
