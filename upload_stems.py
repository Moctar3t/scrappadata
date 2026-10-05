import os
import sys
import urllib.request
import urllib.parse
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configurations
STORAGE_ZONE = "videobb"
# The storage zone key comes from the environment, never from this file.
ACCESS_KEY = os.environ.get("BUNNY_STORAGE_PASSWORD") or sys.exit("set BUNNY_STORAGE_PASSWORD")
BASE_URL = f"https://storage.bunnycdn.com/{STORAGE_ZONE}/"
LOCAL_DIR = os.path.join(os.environ['USERPROFILE'], 'Desktop', 'DIME_STEM_PIPELINE', 'stems', 'htdemucs_ft')
CACHE_FILE = os.path.join(os.environ['USERPROFILE'], 'Desktop', 'DIME_STEM_PIPELINE', 'uploaded_cache.json')
STEM_FILES = ["bass.mp3", "drums.mp3", "other.mp3", "vocals.mp3"]
CONCURRENT_UPLOADS = 12

cache_lock = threading.Lock()
uploaded_cache = set()

# Load cache
if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            uploaded_cache = set(json.load(f))
        print(f"Loaded cache with {len(uploaded_cache)} previously uploaded files.")
    except Exception as e:
        print(f"Error loading cache: {e}")
else:
    print("No previous cache found. Starting fresh.")

def mark_as_uploaded(remote_path):
    with cache_lock:
        uploaded_cache.add(remote_path)
        try:
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(list(uploaded_cache), f, indent=2)
        except Exception as e:
            print(f"Error saving cache: {e}")

def upload_single_file(local_path, remote_path):
    # Skip if already uploaded
    if "Drake - Janice" in remote_path or "disiz (Ft. Theodora) - melodrama" in remote_path:
        # Force upload for these songs to update stems on CDN
        pass
    elif remote_path in uploaded_cache:
        return True, local_path, None, True

    url = BASE_URL + urllib.parse.quote(remote_path.replace("\\", "/"), safe="/")
    headers = {
        "AccessKey": ACCESS_KEY,
        "Content-Type": "application/octet-stream"
    }
    
    # Debug print for first few files
    if "test" in remote_path or hash(remote_path) % 100 == 0:
        print(f"[DEBUG] Path: {remote_path} -> URL: {url}")
        print(f"[DEBUG] Headers Keys: {list(headers.keys())}")
    
    # Retry logic
    for attempt in range(3):
        try:
            with open(local_path, 'rb') as f:
                data = f.read()
            
            req = urllib.request.Request(url, data=data, headers=headers, method='PUT')
            with urllib.request.urlopen(req, timeout=30.0) as response:
                if response.status in [200, 201]:
                    mark_as_uploaded(remote_path)
                    return True, local_path, None, False
        except Exception as e:
            if attempt == 2:
                return False, local_path, str(e), False
            time.sleep(1.0)
    return False, local_path, "Unknown error", False

def main():
    print("=== Dimé Stems CDN Uploader ===")
    if not os.path.exists(LOCAL_DIR):
        print(f"Error: Local stems directory {LOCAL_DIR} does not exist.")
        sys.exit(1)
        
    folders = [f for f in os.listdir(LOCAL_DIR) if os.path.isdir(os.path.join(LOCAL_DIR, f))]
    print(f"Found {len(folders)} separated track folders to process.")
    
    upload_tasks = []
    total_stems_found = 0
    skipped_count = 0
    
    for folder in folders:
        folder_path = os.path.join(LOCAL_DIR, folder)
        for stem in STEM_FILES:
            stem_path = os.path.join(folder_path, stem)
            if os.path.exists(stem_path):
                # Remote relative path: folder/stem.mp3
                remote_path = f"{folder}/{stem}"
                total_stems_found += 1
                if remote_path in uploaded_cache and "Drake - Janice" not in remote_path and "disiz (Ft. Theodora) - melodrama" not in remote_path:
                    skipped_count += 1
                else:
                    upload_tasks.append((stem_path, remote_path))
                
    print(f"Discovered {total_stems_found} total stem files across folders.")
    print(f"Skipping {skipped_count} files already marked as uploaded in cache.")
    print(f"Files remaining to upload: {len(upload_tasks)}")
    
    if not upload_tasks:
        print("All stems are already uploaded! Nothing to do.")
        sys.exit(0)
        
    print(f"Starting upload pipeline with {CONCURRENT_UPLOADS} concurrent workers...")
    
    success_count = skipped_count
    new_success_count = 0
    fail_count = 0
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=CONCURRENT_UPLOADS) as executor:
        futures = {
            executor.submit(upload_single_file, local, remote): (local, remote)
            for local, remote in upload_tasks
        }
        
        last_progress_time = time.time()
        completed = 0
        total_to_process = len(upload_tasks)
        
        for future in as_completed(futures):
            success, local_path, err, is_skipped = future.result()
            completed += 1
            if success:
                success_count += 1
                if not is_skipped:
                    new_success_count += 1
            else:
                fail_count += 1
                print(f"Failed to upload {os.path.basename(local_path)}: {err}")
                
            # Log progress every 10 seconds or every 50 completed files
            if time.time() - last_progress_time > 10.0 or completed % 50 == 0:
                elapsed = time.time() - start_time
                speed = completed / elapsed if elapsed > 0 else 0
                pct = ((completed + skipped_count) / total_stems_found) * 100
                print(f"Progress: {completed + skipped_count}/{total_stems_found} ({pct:.1f}%) | Success (Total): {success_count} | New Uploads: {new_success_count} | Failures: {fail_count} | Speed: {speed:.1f} files/sec")
                last_progress_time = time.time()
                
    total_time = time.time() - start_time
    print("\n=== Upload Pipeline Finished ===")
    print(f"Total Stems: {total_stems_found}")
    print(f"Previously Uploaded (Skipped): {skipped_count}")
    print(f"Newly Uploaded: {new_success_count}")
    print(f"Failed Uploads: {fail_count}")
    print(f"Total Time: {total_time:.1f} seconds ({(total_time/60):.2f} minutes)")
    
if __name__ == "__main__":
    main()
