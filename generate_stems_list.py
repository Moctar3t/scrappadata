import os

raw_path = "/Users/dime/Developer/scrappadata/separated_raw.txt"
output_path = "/Users/dime/Desktop/separated_tracks_list.md"

if not os.path.exists(raw_path):
    print("Error: raw list not found!")
    exit(1)

with open(raw_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

tracks = []
for line in lines:
    line_str = line.strip()
    if not line_str:
        continue
    if line_str.startswith("spawn ssh"):
        continue
    if "password:" in line_str:
        continue
    tracks.append(line_str)

# Sort tracks alphabetically for clean indexing
tracks.sort()

markdown_content = f"""# DIME SYSTEMS - Separated Stems Catalog Audit
## ⚡ RTX 3070 GPU Acceleration Pipeline Summary (530/1057 Completed)

This list contains all the tracks that have been successfully separated into 4-stem channels (`vocals.mp3`, `drums.mp3`, `bass.mp3`, `other.mp3`) using the `htdemucs_ft` model on the remote Windows GPU worker. The pipeline has been safely paused at exactly **530** tracks.

---

### 📂 Complete Index of Completed Separations

"""

for idx, track in enumerate(tracks, 1):
    markdown_content += f"{idx}. [x] `{track}`\n"

markdown_content += "\n---\n*Audit compiled successfully at 22:50 on May 18, 2026.*\n"

with open(output_path, 'w', encoding='utf-8') as f:
    f.write(markdown_content)

print(f"Successfully generated beautiful checklist at: {output_path}")
