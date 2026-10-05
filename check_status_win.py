import glob
import os
import sys

user_profile = os.environ.get('USERPROFILE')
if not user_profile:
    print("COMPLETED:0")
    print("REMAINING:0")
    sys.exit(0)

tracks_dir = os.path.join(user_profile, "Desktop", "DIME_STEM_PIPELINE", "tracks")
stems_dir = os.path.join(user_profile, "Desktop", "DIME_STEM_PIPELINE", "tracks", "separated", "htdemucs_ft")

all_mp3s = glob.glob(os.path.join(tracks_dir, "*.mp3"))
completed = 0
for m in all_mp3s:
    song_name = os.path.splitext(os.path.basename(m))[0]
    vocals_path = os.path.join(stems_dir, song_name, "vocals.mp3")
    if os.path.exists(vocals_path) and os.path.getsize(vocals_path) > 0:
        completed += 1

print(f"COMPLETED:{completed}")
print(f"REMAINING:{len(all_mp3s) - completed}")
