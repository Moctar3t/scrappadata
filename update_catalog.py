import json
import re

cards_path = "/Users/dime/Developer/scrappadata/metadata_cards.json"
records_path = "/Users/dime/Developer/scrappadata/metadata_records.json"
catalog_path = "/Users/dime/Desktop/AZ-Systems-Hub-2026-04-06/src/data/music-catalog.ts"

with open(cards_path) as f:
    cards = json.load(f)

with open(records_path) as f:
    records = json.load(f)

with open(catalog_path, 'r') as f:
    content = f.read()

# Generate new cards TS
new_cards_ts = ""
for c in cards:
    new_cards_ts += f'  {{ id: "{c["id"]}", title: "{c["title"]}", artist: "{c["artist"]}", coverArt: "{c["coverArt"]}", duration: "{c["duration"]}" }},\n'

# Generate new records TS
new_records_ts = ""
for r in records:
    new_records_ts += f'  "{r["id"]}": {{ id: "{r["id"]}", title: "{r["title"]}", artist: "{r["artist"]}", duration: {r["duration"]}, bpm: {r["bpm"]}, key: "{r["key"]}", year: "{r["year"]}" }},\n'

# Inject into arrays/objects
content = re.sub(r'(export const songsList: SongCard\[\] = \[)(.*?)(];)', lambda m: m.group(1) + m.group(2) + new_cards_ts + m.group(3), content, flags=re.DOTALL)
content = re.sub(r'(export const songsRecord: Record<string, SongRecord> = \{)(.*?)(\};)', lambda m: m.group(1) + m.group(2) + new_records_ts + m.group(3), content, flags=re.DOTALL)

with open(catalog_path, 'w') as f:
    f.write(content)
print("Catalog updated successfully.")
