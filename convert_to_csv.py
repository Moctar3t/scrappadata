import json, csv

with open("candidates.json", "r") as f:
    cands = json.load(f)

with open("candidates.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["Track Name", "Artist Name"])
    for c in cands:
        # We only care about Wikipedia tracks which need urls (but French/CityPop without explicit yt-dlp parsing is fine too, 
        # ingest_playlist.py will just resolve them properly and skip them if they are dupes).
        writer.writerow([c.get("title", ""), c.get("artist", "")])

print("Created candidates.csv")
