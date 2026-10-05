import json, urllib.request, urllib.parse, subprocess

print("Downloading original audio via yt-dlp...")
subprocess.run(["yt-dlp", "-x", "--audio-format", "mp3", "ytsearch1:Estelle American Boy", "-o", "/tmp/test_original.%(ext)s"], capture_output=True)

print("Fingerprinting...")
r = subprocess.run(["fpcalc", "-json", "/tmp/test_original.mp3"], capture_output=True, text=True)
data = json.loads(r.stdout)
fp, dur = data["fingerprint"], data["duration"]

print(f"Duration: {dur}s")
params = urllib.parse.urlencode({"client":"EDC8Wnn65f","fingerprint":fp,"duration":int(dur),"meta":"recordings"})
resp = json.loads(urllib.request.urlopen(f"https://api.acoustid.org/v2/lookup?{params}", timeout=15).read())

results = resp.get("results", [])
if results:
    for res in results[:1]:
        score = res.get("score")
        recs = res.get("recordings", [])
        print(f"Match! Score: {score}, MB Recordings: {len(recs)}")
        for rec in recs[:3]:
            artists = ", ".join(a.get("name","") for a in rec.get("artists",[]))
            print(f"  -> {artists} - {rec.get('title')}")
else:
    print("No match")
