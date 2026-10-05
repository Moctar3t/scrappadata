import json, os, re, urllib.request, subprocess
from bs4 import BeautifulSoup

def ntitle(t): return re.sub(r'[^a-z0-9]', '', (t or '').lower())
def artist_tokens(a):
    a = (a or '').lower()
    a = re.sub(r'\(feat.*?\)|\[feat.*?\]|feat\.?|ft\.?|featuring', '', a)
    return {re.sub(r'[^a-z0-9]', '', p) for p in re.split(r'[;,&]| x | and ', a) if re.sub(r'[^a-z0-9]', '', p)}

APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
SONGS = os.path.join(APP, "songs")
catalog_nt_at = []
import glob
for fp in glob.glob(os.path.join(SONGS, "*.json")):
    try:
        s = json.load(open(fp, encoding="utf-8"))
        catalog_nt_at.append((ntitle(s.get("title", "")), artist_tokens(s.get("artist", ""))))
    except Exception: pass

def is_duplicate(title, artist):
    nt = ntitle(title)
    at = artist_tokens(artist)
    for cnt, cat in catalog_nt_at:
        if nt == cnt and (not at or not cat or (at & cat)): return True
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
                    if "scope" in cols[0].attrs and cols[0].name == "th": title_col, artist_col = cols[1], cols[2]
                    else: title_col, artist_col = cols[1], cols[2]
                    title = title_col.get_text(strip=True).strip('"')
                    artist = artist_col.get_text(strip=True)
                    if not is_duplicate(title, artist):
                        candidates.append({"title": title, "artist": artist, "source": f"Billboard {year}"})
                        catalog_nt_at.append((ntitle(title), artist_tokens(artist)))
    except Exception as e: print(f"Error fetching {year}: {e}")

def extract_search(query, source_name, count_limit=150):
    cmd = ["python3", "-m", "yt_dlp", "--flat-playlist", "--dump-json", f"ytsearch{count_limit}:{query}"]
    try: output = subprocess.check_output(cmd, text=True)
    except subprocess.CalledProcessError: return
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
                if not is_duplicate(track_title, artist):
                    candidates.append({"title": track_title, "artist": artist, "source": source_name, "url": data.get("url")})
                    catalog_nt_at.append((ntitle(track_title), artist_tokens(artist)))
        except json.JSONDecodeError: pass

extract_search("french rap hits 2023 2024 -mix -playlist", "French Hits", 100)
extract_search("80s japanese city pop songs -mix -playlist", "City Pop", 60)

billboard = [c for c in candidates if c["source"].startswith("Billboard")]
city_pop = [c for c in candidates if c["source"] == "City Pop"]
french = [c for c in candidates if c["source"] == "French Hits"]

# We want 50 city pop
city_pop = city_pop[:50]
# Total non-city-pop should be 500
non_city_pop = french + billboard
non_city_pop = non_city_pop[:500]

final_list = city_pop + non_city_pop
with open("candidates.json", "w") as f: json.dump(final_list, f, indent=2)
print(f"City pop: {len(city_pop)}, Others: {len(non_city_pop)}, Total: {len(final_list)}")
