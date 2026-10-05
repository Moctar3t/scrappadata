import re

catalog_path = "/Users/dime/Desktop/AZ-Systems-Hub-2026-04-06/src/data/music-catalog.ts"
win_tracks_path = "/Users/dime/Developer/scrappadata/win_tracks_list.txt"

# 1. Parse catalog
catalog_songs = []
with open(catalog_path, "r", encoding="utf-8") as f:
    content = f.read()

matches = re.findall(r'title:\s*"([^"]+)",\s*artist:\s*"([^"]+)"', content)
for title, artist in matches:
    catalog_songs.append({
        'title': title.strip().lower(),
        'artist': artist.strip().lower()
    })

# Deduplicate catalog
unique_catalog = []
seen = set()
for item in catalog_songs:
    k = (item['title'], item['artist'])
    if k not in seen:
        seen.add(k)
        unique_catalog.append(item)

# 2. Parse Windows tracks list from file
win_tracks = []
with open(win_tracks_path, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line.endswith(".mp3"):
            win_tracks.append(line)

def normalize_text(text):
    text = text.lower()
    text = re.sub(r'\(feat\..*?\)', '', text)
    text = re.sub(r'\(with.*?\)', '', text)
    text = re.sub(r'feat\..*', '', text)
    text = re.sub(r'with\..*', '', text)
    text = re.sub(r'[^a-z0-9]', '', text)
    return text.strip()

# Find matches to delete
to_delete = []
for file_name in win_tracks:
    # File name format: "Artist - Title.mp3"
    name_no_ext = file_name[:-4]
    if " - " not in name_no_ext:
        continue
    parts = name_no_ext.split(" - ", 1)
    f_artist = normalize_text(parts[0])
    f_title = normalize_text(parts[1])
    
    matched = False
    for cat in unique_catalog:
        cat_title = normalize_text(cat['title'])
        cat_artist = normalize_text(cat['artist'])
        
        # Loose match title & artist
        if (f_title == cat_title or f_title in cat_title or cat_title in f_title) and \
           (f_artist in cat_artist or cat_artist in f_artist):
            matched = True
            break
            
    if matched:
        to_delete.append(file_name)

print(f"Found {len(to_delete)} files to delete.")

# Generate standard CMD batch commands using %USERPROFILE%
cmd_script = "@echo off\n"
for file in to_delete:
    cmd_script += f'del /f /q "%USERPROFILE%\\Desktop\\DIME_STEM_PIPELINE\\tracks\\{file}"\n'

with open("/Users/dime/Developer/scrappadata/delete_catalog_tracks.bat", "w", encoding="utf-8") as out:
    out.write(cmd_script)

print("Generated delete_catalog_tracks.bat using USERPROFILE")
