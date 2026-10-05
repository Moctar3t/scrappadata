import json, csv, time, urllib.request, urllib.parse, subprocess

def get_itunes_duration(title, artist):
    query = f"{artist} {title}"
    url = "https://itunes.apple.com/search?term=" + urllib.parse.quote(query) + "&entity=song&limit=1"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
            if data.get("resultCount", 0) > 0:
                return data["results"][0].get("trackTimeMillis", 0)
    except Exception:
        pass
    return 0

def get_yt_duration(url):
    try:
        res = subprocess.run(["yt-dlp", "--print", "duration", url], capture_output=True, text=True, check=True)
        dur = float(res.stdout.strip())
        return int(dur * 1000)
    except Exception:
        return 0

def main():
    with open("candidates.json", "r") as f:
        cands = json.load(f)

    enriched = []
    fieldnames = ["Track Name", "Artist Name(s)", "Duration (ms)"]

    for i, c in enumerate(cands):
        title = c.get("title", "")
        artist = c.get("artist", "")
        url = c.get("url", "")
        
        dur_ms = 0
        if url:
            dur_ms = get_yt_duration(url)
            print(f"[{i}/{len(cands)}] YT-DLP: {artist} - {title} -> {dur_ms}ms")
        else:
            dur_ms = get_itunes_duration(title, artist)
            print(f"[{i}/{len(cands)}] iTunes: {artist} - {title} -> {dur_ms}ms")
            time.sleep(0.1)

        enriched.append({
            "Track Name": title,
            "Artist Name(s)": artist,
            "Duration (ms)": dur_ms
        })

    with open("candidates_enriched.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(enriched)
    print("Done generating candidates_enriched.csv")

if __name__ == '__main__':
    main()
