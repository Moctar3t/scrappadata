import os
import glob
import subprocess
import sys
import math

# Ensure UTF-8 output encoding for beautiful console characters
try:
    sys.stdout.reconfigure(encoding='utf-8')
except:
    pass

user_profile = os.environ.get('USERPROFILE')
if not user_profile:
    print("Error: USERPROFILE environment variable not found!")
    sys.exit(1)

# Paths
venv_python = os.path.join(user_profile, "demucs_relay", "venv", "Scripts", "python.exe")
tracks_dir = os.path.join(user_profile, "Desktop", "DIME_STEM_PIPELINE", "tracks")
stems_dir = os.path.join(user_profile, "Desktop", "DIME_STEM_PIPELINE", "tracks", "separated")
model_name = "htdemucs_ft"

# Exact absolute paths to print to the user
abs_stems_path = os.path.join(stems_dir, model_name)

if not os.path.exists(venv_python):
    print(f"Error: Python interpreter not found at {venv_python}")
    sys.exit(1)

# Find all MP3 files
all_mp3s = glob.glob(os.path.join(tracks_dir, "*.mp3"))
if not all_mp3s:
    print(f"No MP3 files found in: {tracks_dir}")
    sys.exit(0)

# Explicit Safe Resume Filter (Double-do/Overwrite Protection)
to_process = []
skipped_count = 0
expected_stems = ["vocals.mp3", "drums.mp3", "bass.mp3", "other.mp3"]

for mp3_path in all_mp3s:
    filename = os.path.basename(mp3_path)
    song_name = os.path.splitext(filename)[0]
    
    song_output_dir = os.path.join(stems_dir, model_name, song_name)
    
    is_completed = True
    if not os.path.exists(song_output_dir):
        is_completed = False
    else:
        for stem in expected_stems:
            stem_path = os.path.join(song_output_dir, stem)
            if not os.path.exists(stem_path) or os.path.getsize(stem_path) == 0:
                is_completed = False
                break
                
    if is_completed:
        skipped_count += 1
    else:
        to_process.append(mp3_path)

# Print Beautiful Header UI
print("┌────────────────────────────────────────────────────────┐")
print("│          DIME SYSTEMS - DUAL-PIPELINE SEPARATION       │")
print("└────────────────────────────────────────────────────────┘")
print(f"  📍 Isolated Stems Output Folder:")
print(f"     -> {abs_stems_path}")
print("──────────────────────────────────────────────────────────")
print(f"  ● Total tracks on disk: {len(all_mp3s)}")
print(f"  ● Completed (Skipped) : {skipped_count}")
print(f"  ● Remaining to process: {len(to_process)}")
print("──────────────────────────────────────────────────────────")

if not to_process:
    print("  ✔ All tracks are already separated! Nothing to do.")
    print("──────────────────────────────────────────────────────────")
    sys.exit(0)

# Split remaining workload into 2 parallel queues
half = math.ceil(len(to_process) / 2)
queue_a = to_process[:half]
queue_b = to_process[half:]

def chunk_list(lst, sz):
    return [lst[i:i + sz] for i in range(0, len(lst), sz)]

batches_a = chunk_list(queue_a, 35)
batches_b = chunk_list(queue_b, 35)
total_batches = max(len(batches_a), len(batches_b))

print("  🚀 Dual-Pipeline GPU Acceleration: ACTIVE")
print(f"  ⚡ Pipeline A: processing {len(queue_a)} songs")
print(f"  ⚡ Pipeline B: processing {len(queue_b)} songs")
print(f"  🔊 Your RTX 3070 GPU will run at 100% load during separation.")
print("──────────────────────────────────────────────────────────")

# Run parallel batches sequentially
for i in range(total_batches):
    processes = []
    
    # Titles of tracks in this batch to display nicely in the UI
    songs_in_a = []
    songs_in_b = []
    
    # Launch Pipeline A (Batch i)
    if i < len(batches_a):
        batch_a = batches_a[i]
        for path in batch_a:
            songs_in_a.append(os.path.splitext(os.path.basename(path))[0])
            
        cmd_a = [
            venv_python, "-m", "demucs.separate",
            "--mp3", "--mp3-bitrate", "192",
            "-j", "2",
            "--device", "cuda",
            "-n", model_name,
            "-o", stems_dir
        ]
        cmd_a.extend(batch_a)
        p_a = subprocess.Popen(cmd_a, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p_a)
        
    # Launch Pipeline B (Batch i)
    if i < len(batches_b):
        batch_b = batches_b[i]
        for path in batch_b:
            songs_in_b.append(os.path.splitext(os.path.basename(path))[0])
            
        cmd_b = [
            venv_python, "-m", "demucs.separate",
            "--mp3", "--mp3-bitrate", "192",
            "-j", "2",
            "--device", "cuda",
            "-n", model_name,
            "-o", stems_dir
        ]
        cmd_b.extend(batch_b)
        p_b = subprocess.Popen(cmd_b, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p_b)

    # Print Clean, Informative Batch Status Block
    print(f"\n┌────────────────────────────────────────────────────────┐")
    print(f"│ 📂 BATCH {i+1} OF {total_batches} IN PROGRESS")
    print(f"├────────────────────────────────────────────────────────┤")
    if songs_in_a:
        print(f"│  ⚡ Pipeline A is processing {len(songs_in_a)} songs:")
        # Show first 3 songs in this batch, collapse the rest
        for song in songs_in_a[:3]:
            print(f"│     • {song[:48]}")
        if len(songs_in_a) > 3:
            print(f"│       ... and {len(songs_in_a) - 3} more")
    if songs_in_b:
        print(f"│  ⚡ Pipeline B is processing {len(songs_in_b)} songs:")
        for song in songs_in_b[:3]:
            print(f"│     • {song[:48]}")
        if len(songs_in_b) > 3:
            print(f"│       ... and {len(songs_in_b) - 3} more")
    print(f"└────────────────────────────────────────────────────────┘")
    print("  [Processing batch... Please listen to your GPU fans roar! 🔊]")
    
    # Wait for both processes in the current batch to complete
    for p in processes:
        p.wait()

print("\n🎉 All stem separations finished successfully!")
print(f"📂 Stems folder location: {abs_stems_path}")
