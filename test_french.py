import json, subprocess, re

def extract_playlist(query):
    print("Searching:", query)
    cmd = ["python3", "-m", "yt_dlp", "--flat-playlist", "--dump-json", f"ytsearch1:{query}"]
    try:
        output = subprocess.check_output(cmd, text=True)
    except subprocess.CalledProcessError as e:
        print("yt-dlp error:", e)
        return

    playlist_url = None
    for line in output.strip().split('\n'):
        if not line: continue
        try:
            data = json.loads(line)
            if 'url' in data:
                playlist_url = data['url']
                break
        except json.JSONDecodeError:
            pass
            
    if not playlist_url:
        print("No URL found")
        return
        
    print("Playlist URL:", playlist_url)
    cmd = ["python3", "-m", "yt_dlp", "--flat-playlist", "--dump-json", playlist_url]
    output = subprocess.check_output(cmd, text=True)
    
    songs = []
    for line in output.strip().split('\n'):
        if not line: continue
        try:
            data = json.loads(line)
            title = data.get("title", "")
            parts = title.split(" - ", 1)
            artist = parts[0] if len(parts) == 2 else "Unknown"
            track_title = parts[1] if len(parts) == 2 else title
            track_title = re.sub(r'\(.*?\)', '', track_title).strip()
            
            if track_title and artist != "Unknown":
                songs.append({"title": track_title, "artist": artist, "source": "French Hits", "url": data.get("url")})
        except json.JSONDecodeError as e:
            print("JSON error:", e)
            
    print(f"Extracted {len(songs)} songs")
    with open("french_hits.json", "w") as f:
        json.dump(songs, f, indent=2)

extract_playlist("top hits france 2023 2024 youtube playlist rap français variété française les plus écoutés")
