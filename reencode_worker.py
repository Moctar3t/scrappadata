# DNS Monkeypatch for storage.bunnycdn.com to bypass broken Windows DNS
import socket
_original_getaddrinfo = socket.getaddrinfo
def custom_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host == "storage.bunnycdn.com":
        return _original_getaddrinfo("109.61.89.53", port, family, type, proto, flags)
    return _original_getaddrinfo(host, port, family, type, proto, flags)
socket.getaddrinfo = custom_getaddrinfo

import os
import sys
import json
import urllib.request
import urllib.parse
import subprocess
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configurations
STORAGE_ZONE = "videobb"
# The storage zone key comes from the environment, never from this file.
ACCESS_KEY = os.environ.get("BUNNY_STORAGE_PASSWORD") or sys.exit("set BUNNY_STORAGE_PASSWORD")
BASE_URL = f"https://storage.bunnycdn.com/{STORAGE_ZONE}/"

# Root working directory on Windows
PIPELINE_DIR = os.path.join(os.environ['USERPROFILE'], 'Desktop', 'DIME_STEM_PIPELINE')
TEMP_DIR = os.path.join(PIPELINE_DIR, 'reencode_temp')
JOBS_FILE = os.path.join(PIPELINE_DIR, 'reencode_jobs.json')
PROGRESS_FILE = os.path.join(PIPELINE_DIR, 'reencode_progress.json')

STEM_NAMES = ["bass", "drums", "other", "vocals"]
CONCURRENT_JOBS = 32

progress_lock = threading.Lock()
reencode_progress = {}

# Ensure UTF-8 output encoding if possible
try:
    sys.stdout.reconfigure(encoding='utf-8')
except:
    pass

# Load progress
if os.path.exists(PROGRESS_FILE):
    try:
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            reencode_progress = json.load(f)
        print(f"Loaded progress: {len(reencode_progress)} previously completed songs.")
    except Exception as e:
        print(f"Error loading progress file: {e}")
else:
    print("No previous progress found. Starting fresh.")

def save_progress(song_id, status, details=None):
    with progress_lock:
        reencode_progress[song_id] = {
            "status": status,
            "timestamp": time.time(),
            **(details or {})
        }
        try:
            with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
                json.dump(reencode_progress, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving progress for {song_id}: {e}")

def process_single_song(job):
    song_id = job["id"]
    folder = job["folder"]
    title = job.get("title", "Unknown")
    artist = job.get("artist", "Unknown")
    
    if song_id in reencode_progress and reencode_progress[song_id].get("status") == "success":
        return True, song_id, "Skipped (already done)", 0, 0

    print(f"[{song_id}] Starting: '{artist} - {title}' (Folder: {folder})")
    
    # Create local temp dir for this song
    song_temp_dir = os.path.join(TEMP_DIR, song_id)
    os.makedirs(song_temp_dir, exist_ok=True)
    
    original_size_total = 0
    new_size_total = 0
    
    try:
        for stem in STEM_NAMES:
            mp3_filename = f"{stem}.mp3"
            m4a_filename = f"{stem}.m4a"
            
            local_mp3 = os.path.join(song_temp_dir, mp3_filename)
            local_m4a = os.path.join(song_temp_dir, m4a_filename)
            
            # --- 1. Download MP3 from CDN ---
            # Remote relative path: folder/stem.mp3
            remote_mp3_path = f"{folder}/{mp3_filename}"
            download_url = BASE_URL + urllib.parse.quote(remote_mp3_path.replace("\\", "/"), safe="/")
            
            headers = {"AccessKey": ACCESS_KEY}
            req = urllib.request.Request(download_url, headers=headers)
            
            downloaded = False
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(req, timeout=30.0) as response:
                        with open(local_mp3, 'wb') as out_f:
                            out_f.write(response.read())
                    downloaded = True
                    break
                except Exception as e:
                    if attempt == 2:
                        raise Exception(f"Failed to download {mp3_filename} from CDN: {e}")
                    time.sleep(1.0)
            
            if not downloaded:
                raise Exception(f"Failed to download {mp3_filename} from CDN after retries.")
            
            orig_size = os.path.getsize(local_mp3)
            original_size_total += orig_size
            
            # --- 2. Convert to M4A using ffmpeg ---
            # -y overwrites, -i input, -c:a aac specifies AAC encoder, -b:a 128k specifies bitrate
            # We redirect output to devnull to avoid cluttering logs
            cmd = ["ffmpeg", "-y", "-i", local_mp3, "-c:a", "aac", "-b:a", "128k", local_m4a]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            except subprocess.CalledProcessError as e:
                raise Exception(f"FFmpeg conversion failed for {mp3_filename}: {e}")
            
            new_size = os.path.getsize(local_m4a)
            new_size_total += new_size
            
            # --- 3. Upload M4A to CDN ---
            remote_m4a_path = f"{folder}/{m4a_filename}"
            upload_url = BASE_URL + urllib.parse.quote(remote_m4a_path.replace("\\", "/"), safe="/")
            
            headers = {
                "AccessKey": ACCESS_KEY,
                "Content-Type": "application/octet-stream"
            }
            
            uploaded = False
            for attempt in range(3):
                try:
                    with open(local_m4a, 'rb') as f:
                        upload_data = f.read()
                    
                    up_req = urllib.request.Request(upload_url, data=upload_data, headers=headers, method='PUT')
                    with urllib.request.urlopen(up_req, timeout=30.0) as response:
                        if response.status in [200, 201]:
                            uploaded = True
                            break
                except Exception as e:
                    if attempt == 2:
                        raise Exception(f"Failed to upload {m4a_filename} to CDN: {e}")
                    time.sleep(1.0)
                    
            if not uploaded:
                raise Exception(f"Failed to upload {m4a_filename} to CDN after retries.")
        
        # Save success progress
        savings_pct = (1 - (new_size_total / original_size_total)) * 100 if original_size_total > 0 else 0
        save_progress(song_id, "success", {
            "title": title,
            "artist": artist,
            "folder": folder,
            "original_size": original_size_total,
            "new_size": new_size_total,
            "savings_pct": savings_pct
        })
        
        # Clean up temp files for this song
        for f in os.listdir(song_temp_dir):
            try:
                os.remove(os.path.join(song_temp_dir, f))
            except:
                pass
        try:
            os.rmdir(song_temp_dir)
        except:
            pass
            
        print(f"[{song_id}] Success: '{artist} - {title}' (Size: {original_size_total/1024/1024:.1f}MB -> {new_size_total/1024/1024:.1f}MB, Savings: {savings_pct:.1f}%)")
        return True, song_id, "Completed", original_size_total, new_size_total

    except Exception as e:
        print(f"[{song_id}] ERROR: {e}")
        save_progress(song_id, "error", {"error": str(e)})
        # Clean up whatever temp files were made
        try:
            for f in os.listdir(song_temp_dir):
                os.remove(os.path.join(song_temp_dir, f))
            os.rmdir(song_temp_dir)
        except:
            pass
        return False, song_id, str(e), 0, 0

def main():
    try:
        import ctypes
        # ES_CONTINUOUS (0x80000000) | ES_SYSTEM_REQUIRED (0x00000001)
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        print("✓ System keep-awake requested.")
    except Exception as e:
        print("Warning: Failed to set thread keep-awake:", e)

    print("=== Dimé Stems CDN Re-encoder Worker ===")
    if not os.path.exists(JOBS_FILE):
        print(f"Error: Jobs file {JOBS_FILE} does not exist. Please run prep script first.")
        sys.exit(1)
        
    with open(JOBS_FILE, 'r', encoding='utf-8') as f:
        jobs = json.load(f)
        
    print(f"Total jobs loaded: {len(jobs)}")
    
    # Create temp directory if it doesn't exist
    os.makedirs(TEMP_DIR, exist_ok=True)
    
    # Filter jobs to process
    jobs_to_run = []
    for job in jobs:
        song_id = job["id"]
        if song_id not in reencode_progress or reencode_progress[song_id].get("status") != "success":
            jobs_to_run.append(job)
            
    print(f"Jobs remaining to process: {len(jobs_to_run)}")
    if not jobs_to_run:
        print("All jobs are already finished!")
        sys.exit(0)
        
    print(f"Starting re-encoding queue with {CONCURRENT_JOBS} concurrent workers...")
    
    start_time = time.time()
    completed = 0
    success_count = 0
    fail_count = 0
    
    total_original_bytes = 0
    total_new_bytes = 0
    
    # Pre-warm status print
    with ThreadPoolExecutor(max_workers=CONCURRENT_JOBS) as executor:
        futures = {executor.submit(process_single_song, job): job for job in jobs_to_run}
        
        for future in as_completed(futures):
            success, song_id, msg, orig_b, new_b = future.result()
            completed += 1
            if success:
                if "Skipped" not in msg:
                    success_count += 1
                    total_original_bytes += orig_b
                    total_new_bytes += new_b
            else:
                fail_count += 1
                
            if completed % 10 == 0 or completed == len(jobs_to_run):
                elapsed = time.time() - start_time
                speed = completed / elapsed if elapsed > 0 else 0
                pct = (completed / len(jobs_to_run)) * 100
                savings_gb = (total_original_bytes - total_new_bytes) / 1024 / 1024 / 1024
                print(f"Progress: {completed}/{len(jobs_to_run)} ({pct:.1f}%) | Successes: {success_count} | Failures: {fail_count} | Saved Space: {savings_gb:.2f} GB | Speed: {speed:.2f} songs/min")
                
    total_time = time.time() - start_time
    savings_gb = (total_original_bytes - total_new_bytes) / 1024 / 1024 / 1024
    print("\n=== Re-encoding Worker Finished ===")
    print(f"Processed: {completed}")
    print(f"Successes: {success_count}")
    print(f"Failures: {fail_count}")
    print(f"Total Bandwidth Saved: {savings_gb:.2f} GB")
    print(f"Total Run Time: {total_time/60:.2f} minutes")

if __name__ == "__main__":
    main()
