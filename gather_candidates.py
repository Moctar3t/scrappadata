import json
import os
import re
import urllib.request
import subprocess
from bs4 import BeautifulSoup

def ntitle(t): return re.sub(r'[^a-z0-9]', '', (t or '').lower())
def artist_tokens(a):
    a = (a or '').lower()
    a = re.sub(r'\(feat.*?\)|\[feat.*?\]|feat\.?|ft\.?|featuring', '', a)
    return {re.sub(r'[^a-z0-9]', '', p) for p in re.split(r'[;,&]| x | and ', a) if re.sub(r'[^a-z0-9]', '', p)}

# Load current catalog
APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
SONGS = os.path.join(APP, "songs")

catalog_nt_at = []
import glob
for fp in glob.glob(os.path.join(SONGS, "*.json")):
    try:
        s = json.load(open(fp, encoding="utf-8"))
        catalog_nt_at.append((ntitle(s.get("title", "")), artist_tokens(s.get("artist", ""))))
    except Exception as e:
        pass

def is_duplicate(title, artist, target_list=catalog_nt_at):
    nt = ntitle(title)
    at = artist_tokens(artist)
    for cnt, cat in target_list:
        if nt == cnt and (not at or not cat or (at & cat)):
            return True
    return False

candidates = []

# Fetch US Billboard
for year in range(2014, 2025):
    try:
        url = f"https://en.wikipedia.org/wiki/Billboard_Year-End_Hot_100_singles_of_{year}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req).read().decode("utf-8")
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_="wikitable")
        if table:
            for row in table.find_all("tr")[1:]:
                cols = row.find_all(["td", "th"])
                if len(cols) >= 3:
                    # Depending on year, format varies. Usually col 1 is title, col 2 is artist
                    # Sometimes col 0 is rank, col 1 is title, col 2 is artist
                    if "scope" in cols[0].attrs and cols[0].name == "th":
                        title_col = cols[1]
                        artist_col = cols[2]
                    else:
                        title_col = cols[1]
                        artist_col = cols[2]
                    
                    title = title_col.get_text(strip=True).strip('"')
                    artist = artist_col.get_text(strip=True)
                    if not is_duplicate(title, artist):
                        candidates.append({"title": title, "artist": artist, "source": f"Billboard {year}"})
                        catalog_nt_at.append((ntitle(title), artist_tokens(artist)))
    except Exception as e:
        print(f"Error fetching {year}: {e}")

# Fetch Youtube playlists
def extract_playlist(query, source_name, count_limit=150):
    try:
        # Use yt-dlp to extract flat playlist
        cmd = ["python3", "-m", "yt_dlp", "--flat-playlist", "--dump-json", f"ytsearch1:{query}"]
        output = subprocess.check_output(cmd, text=True)
        playlist_url = None
        for line in output.strip().split('\\n'):
            data = json.loads(line)
            if 'url' in data:
                playlist_url = data['url']
                break
        
        if not playlist_url:
            print(f"Playlist not found for: {query}")
            return
            
        cmd = ["python3", "-m", "yt_dlp", "--flat-playlist", "--dump-json", playlist_url]
        output = subprocess.check_output(cmd, text=True)
        count = 0
        for line in output.strip().split('\\n'):
            if not line: continue
            data = json.loads(line)
            title = data.get("title", "")
            # Try to split by ' - ' or just use the title
            parts = title.split(" - ", 1)
            if len(parts) == 2:
                artist, track_title = parts[0], parts[1]
            else:
                artist = "Unknown"
                track_title = title
            
            # Clean up things like "(Clip officiel)"
            track_title = re.sub(r'\(.*?\)', '', track_title).strip()
            
            if track_title and artist != "Unknown":
                if not is_duplicate(track_title, artist):
                    candidates.append({"title": track_title, "artist": artist, "source": source_name, "url": data.get("url")})
                    catalog_nt_at.append((ntitle(track_title), artist_tokens(artist)))
                    count += 1
            if count >= count_limit:
                break
    except Exception as e:
        print(f"Error extracting {query}: {e}")

extract_playlist("top hits france 2023 2024 youtube playlist rap français variété française les plus écoutés", "French Hits", count_limit=100)
extract_playlist("best city pop youtube playlist classic 80s japanese citypop full playlist", "City Pop", count_limit=50)

# Trim Billboard to get exactly 500 total if possible
billboard = [c for c in candidates if c["source"].startswith("Billboard")]
others = [c for c in candidates if not c["source"].startswith("Billboard")]

# We want 500 total, meaning 500 - len(others) billboard songs
billboard = billboard[:(500 - len(others))]

candidates = others + billboard

with open("candidates.json", "w") as f:
    json.dump(candidates, f, indent=2)

print(f"Found {len(candidates)} candidates.")
