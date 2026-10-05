import json, subprocess, re

def extract_search(query, source_name, count_limit=150):
    cmd = ["python3", "-m", "yt_dlp", "--flat-playlist", "--dump-json", f"ytsearch{count_limit}:{query}"]
    try:
        output = subprocess.check_output(cmd, text=True)
    except subprocess.CalledProcessError as e:
        print("yt-dlp error:", e)
        return []

    songs = []
    for line in output.strip().split('\n'):
        if not line: continue
        try:
            data = json.loads(line)
            title = data.get("title", "")
            parts = title.split(" - ", 1)
            artist = parts[0] if len(parts) == 2 else "Unknown"
            track_title = parts[1] if len(parts) == 2 else title
            track_title = re.sub(r'\(.*?\)|\[.*?\]', '', track_title).strip()
            
            if track_title and artist != "Unknown" and "mix" not in title.lower() and "playlist" not in title.lower():
                songs.append({"title": track_title, "artist": artist, "source": source_name, "url": data.get("url")})
        except json.JSONDecodeError:
            pass
            
    print(f"Extracted {len(songs)} songs for {source_name}")
    return songs

songs = extract_search("french rap hits 2023 2024", "French Hits", 100)
with open("french_hits.json", "w") as f:
    json.dump(songs, f, indent=2)
