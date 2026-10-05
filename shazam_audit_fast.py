import asyncio
import json
import os
import urllib.request
import time
from shazamio import Shazam

state_path = "/tmp/ingest_gpu_state.json"
try:
    state = json.load(open(state_path, encoding="utf-8"))
    by_id = {str(t.get("catalog_id")): t for t in state["tracks"] if t.get("catalog_id")}
except:
    by_id = {}

try:
    idx = json.loads(urllib.request.urlopen("https://dimesystemsstorage.b-cdn.net/musicdata/music-index.json").read())
    for s in idx.get("songsList", []):
        cid = str(s["id"])
        if cid not in by_id:
            by_id[cid] = {"artist": s.get("artist"), "title": s.get("title")}
except:
    pass

def normalize(s):
    import re, unicodedata
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()

async def process_file(shazam, cid, path, our_track, mismatches, sem):
    async with sem:
        our_label = f"{our_track.get('artist')} - {our_track.get('title')}"
        our_artist_norm = normalize(our_track.get("artist"))
        our_title_norm = normalize(our_track.get("title"))
        
        try:
            out = await shazam.recognize_song(path)
            track = out.get("track")
        except Exception as e:
            return f"  ! ID {cid}: API Error - {e}"
            
        if not track:
            mismatches.append({
                "id": cid,
                "our_artist": our_track.get("artist"),
                "our_title": our_track.get("title"),
                "shazam_artist": None,
                "shazam_title": None,
                "reason": "NO SHAZAM MATCH"
            })
            return f"  · ID {cid}: NO SHAZAM MATCH"
            
        sh_artist = track.get("subtitle", "")
        sh_title = track.get("title", "")
        sh_artist_norm = normalize(sh_artist)
        sh_title_norm = normalize(sh_title)
        
        our_title_words = set(our_title_norm.split())
        sh_title_words = set(sh_title_norm.split())
        our_artist_words = set(our_artist_norm.split())
        sh_artist_words = set(sh_artist_norm.split())
        
        title_match = (our_title_norm in sh_title_norm or sh_title_norm in our_title_norm or len(our_title_words & sh_title_words) > 0)
        artist_match = (our_artist_norm in sh_artist_norm or sh_artist_norm in our_artist_norm or len(our_artist_words & sh_artist_words) > 0)
        
        if title_match and artist_match:
            return f"  ✓ ID {cid}: MATCH"
        else:
            mismatches.append({
                "id": cid,
                "our_artist": our_track.get("artist"),
                "our_title": our_track.get("title"),
                "shazam_artist": sh_artist,
                "shazam_title": sh_title,
                "reason": "METADATA MISMATCH"
            })
            return f"  ✗ ID {cid}: MISMATCH! Ours: {our_label} | Shazam: {sh_artist} - {sh_title}"


async def audit():
    shazam = Shazam()
    previews_dir = "/var/www/zingy-previews/"
    
    files = [f for f in os.listdir(previews_dir) if f.endswith(".m4a")]
    files.sort(key=lambda x: int(x.split(".")[0]) if x.split(".")[0].isdigit() else 0)
    
    mismatches = []
    print(f"Starting FAST audit of {len(files)} tracks...")
    
    # Process 25 tracks concurrently
    sem = asyncio.Semaphore(25)
    
    chunk_size = 100
    start_time = time.time()
    
    for i in range(0, len(files), chunk_size):
        chunk = files[i:i+chunk_size]
        tasks = []
        for fname in chunk:
            cid = fname.split(".")[0]
            path = os.path.join(previews_dir, fname)
            our_track = by_id.get(cid)
            if our_track:
                tasks.append(process_file(shazam, cid, path, our_track, mismatches, sem))
        
        results = await asyncio.gather(*tasks)
        for res in results:
            if "MISMATCH!" in res:
                print(res) # Only print mismatches to keep log clean
        
        print(f"[{min(i+chunk_size, len(files))}/{len(files)}] Processed chunk in {time.time()-start_time:.1f}s...")
        start_time = time.time()
        
        with open("/tmp/shazam_mismatches.json", "w") as f:
            json.dump(mismatches, f, indent=2)

asyncio.run(audit())
