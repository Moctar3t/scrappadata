import json, os, re, urllib.request, subprocess

APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
SONGS = os.path.join(APP, "songs")
catalog_nt_at = []
import glob
for fp in glob.glob(os.path.join(SONGS, "*.json")):
    try:
        s = json.load(open(fp, encoding="utf-8"))
        catalog_nt_at.append((re.sub(r'[^a-z0-9]', '', (s.get("title", "") or '').lower()), re.sub(r'\(feat.*?\)|\[feat.*?\]|feat\.?|ft\.?|featuring', '', (s.get("artist", "") or '').lower())))
    except Exception: pass

with open("candidates.json", "r") as f:
    candidates = json.load(f)

city_pop_current = [c for c in candidates if c["source"] == "City Pop"]
print("Current City Pop:", len(city_pop_current))

cmd = ["python3", "-m", "yt_dlp", "--flat-playlist", "--dump-json", "ytsearch200:japanese city pop 1980s"]
try: output = subprocess.check_output(cmd, text=True)
except subprocess.CalledProcessError: output = ""

new_city_pop = []
for line in output.strip().split('\n'):
    if not line: continue
    try:
        data = json.loads(line)
        title = data.get("title", "")
        duration = data.get("duration", 0)
        # Skip long videos (mixes usually > 10 mins / 600s)
        if duration and duration > 600: continue
        parts = title.split(" - ", 1)
        artist = parts[0] if len(parts) == 2 else "Unknown"
        track_title = parts[1] if len(parts) == 2 else title
        track_title = re.sub(r'\(.*?\)|\[.*?\]', '', track_title).strip()
        
        # simple dedup
        is_dup = False
        nt = re.sub(r'[^a-z0-9]', '', track_title.lower())
        for cnt, cat in catalog_nt_at:
            if nt == cnt: is_dup = True
        
        if track_title and artist != "Unknown" and not is_dup:
            new_city_pop.append({"title": track_title, "artist": artist, "source": "City Pop", "url": data.get("url")})
            catalog_nt_at.append((nt, artist.lower()))
    except json.JSONDecodeError: pass

print(f"Extracted {len(new_city_pop)} additional City Pop")
# Combine and trim to 50
total_cp = city_pop_current + new_city_pop
total_cp = total_cp[:50]

final = [c for c in candidates if c["source"] != "City Pop"] + total_cp
with open("candidates.json", "w") as f: json.dump(final, f, indent=2)
print("Saved candidates.json. Total:", len(final))
