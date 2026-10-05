import csv

csv_path = "/Users/dime/Downloads/Liked_Songs.csv"
output_path = "/Users/dime/Desktop/DIME_STEM_PIPELINE.nosync/full_spotify_tracklist.txt"

tracks = []
with open(csv_path, "r", encoding="utf-8") as f:
    reader = csv.reader(f)
    header = next(reader)
    # Track Name is at index 1, Artist Name(s) is at index 3
    for row in reader:
        if len(row) > 3:
            track_name = row[1].strip()
            artist_name = row[3].strip()
            if track_name and artist_name:
                tracks.append(f"{artist_name} - {track_name}")

with open(output_path, "w", encoding="utf-8") as out:
    for track in tracks:
        out.write(track + "\n")

print(f"Successfully extracted {len(tracks)} tracks from {csv_path} to {output_path}")
