import os
import csv
import re

csv_path = "/Users/dime/Downloads/Liked_Songs.csv"
catalog_path = "/Users/dime/Desktop/AZ-Systems-Hub-2026-04-06/src/data/music-catalog.ts"
output_path = "/Users/dime/Desktop/DIME_STEM_PIPELINE.nosync/filtered_spotify_tracklist.txt"

# 1. Parse current catalog songs
catalog_songs = []
with open(catalog_path, "r", encoding="utf-8") as f:
    content = f.read()
    
# Extract SongCard items from songsList array
# Format: { id: "...", title: "...", artist: "...", coverArt: "...", duration: "..." }
matches = re.findall(r'title:\s*"([^"]+)",\s*artist:\s*"([^"]+)"', content)
for title, artist in matches:
    catalog_songs.append({
        'title': title.strip(),
        'artist': artist.strip()
    })

print(f"Loaded {len(catalog_songs)} songs from the Hub catalog.")

def normalize_text(text):
    text = text.lower()
    # Remove features: (feat. ...), (with ...), feat. ...
    text = re.sub(r'\(feat\..*?\)', '', text)
    text = re.sub(r'\(with.*?\)', '', text)
    text = re.sub(r'feat\..*', '', text)
    text = re.sub(r'with\..*', '', text)
    # Remove punctuation & spaces
    text = re.sub(r'[^a-z0-9]', '', text)
    return text.strip()

# Normalize catalog for easy matching
normalized_catalog = []
for song in catalog_songs:
    norm_title = normalize_text(song['title'])
    norm_artist = normalize_text(song['artist'])
    normalized_catalog.append((norm_title, norm_artist))

# 2. Parse Liked Songs CSV and deduplicate
skipped_catalog = 0
skipped_duplicates = 0
added_tracks = []
seen_tracks = set()

with open(csv_path, "r", encoding="utf-8") as f:
    reader = csv.reader(f)
    header = next(reader)
    for row in reader:
        if len(row) > 3:
            title = row[1].strip()
            # Artist field in Spotify CSV can contain multiple artists separated by ";"
            artists_str = row[3].strip()
            
            if not title or not artists_str:
                continue
                
            # Deduplicate within the CSV list itself
            track_key = (title.lower(), artists_str.lower())
            if track_key in seen_tracks:
                skipped_duplicates += 1
                continue
            seen_tracks.add(track_key)
            
            # Check against catalog (using smart normalization)
            norm_title = normalize_text(title)
            # Match if any artist in the CSV list matches the catalog artist
            csv_artists = [normalize_text(a) for a in artists_str.split(';')]
            
            is_in_catalog = False
            for cat_title, cat_artist in normalized_catalog:
                # If titles match closely
                if norm_title == cat_title or norm_title in cat_title or cat_title in norm_title:
                    # And at least one artist overlaps
                    if any(a in cat_artist or cat_artist in a for a in csv_artists if a):
                        is_in_catalog = True
                        break
            
            if is_in_catalog:
                skipped_catalog += 1
                continue
                
            # If not in catalog, we format and add it
            # We keep the primary artist or all artists cleanly formatted
            artists_formatted = artists_str.replace(";", ", ")
            added_tracks.append((artists_formatted, title))

# 3. Write output file
with open(output_path, "w", encoding="utf-8") as out:
    for artist, title in added_tracks:
        out.write(f"{artist} - {title}\n")

print(f"--- DEDUPLICATION RESULTS ---")
print(f"Total songs in Liked Songs CSV: {len(seen_tracks) + skipped_duplicates}")
print(f"Duplicate entries inside CSV skipped: {skipped_duplicates}")
print(f"Songs already in your Hub catalog skipped: {skipped_catalog}")
print(f"New unique songs ready for pipeline: {len(added_tracks)}")
print(f"Saved filtered list to: {output_path}")
