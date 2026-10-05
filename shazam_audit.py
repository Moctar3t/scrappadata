import asyncio
import json
import os
import urllib.request
from shazamio import Shazam

# Load state to get our metadata
state_path = "/tmp/ingest_gpu_state.json"
try:
    state = json.load(open(state_path, encoding="utf-8"))
    by_id = {str(t.get("catalog_id")): t for t in state["tracks"] if t.get("catalog_id")}
except Exception as e:
    print(f"Failed to load state file: {e}")
    by_id = {}

# Fallback: Load music index if state doesn't have it (for old tracks)
try:
    idx = json.loads(urllib.request.urlopen("https://dimesystemsstorage.b-cdn.net/musicdata/music-index.json").read())
    for s in idx.get("songsList", []):
        cid = str(s["id"])
        if cid not in by_id:
            by_id[cid] = {"artist": s.get("artist"), "title": s.get("title")}
except Exception as e:
    print(f"Failed to load music index: {e}")

def normalize(s):
    import re, unicodedata
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()

async def audit():
    shazam = Shazam()
    previews_dir = "/var/www/zingy-previews/"
    
    files = [f for f in os.listdir(previews_dir) if f.endswith(".m4a")]
    files.sort(key=lambda x: int(x.split(".")[0]) if x.split(".")[0].isdigit() else 0)
    
    mismatches = []
    
    print(f"Starting audit of {len(files)} tracks...")
    
    for i, fname in enumerate(files, 1):
        cid = fname.split(".")[0]
        path = os.path.join(previews_dir, fname)
        
        our_track = by_id.get(cid)
        if not our_track:
            print(f"[{i}/{len(files)}] ID {cid}: Not in state/index, skipping")
            continue
            
        our_label = f"{our_track.get('artist')} - {our_track.get('title')}"
        our_artist_norm = normalize(our_track.get("artist"))
        our_title_norm = normalize(our_track.get("title"))
        
        try:
            out = await shazam.recognize_song(path)
            track = out.get("track")
        except Exception as e:
            print(f"[{i}/{len(files)}] ID {cid}: API Error - {e}")
            await asyncio.sleep(2)
            continue
            
        if not track:
            print(f"[{i}/{len(files)}] ID {cid}: {our_label} -> NO SHAZAM MATCH")
            mismatches.append({
                "id": cid,
                "our_artist": our_track.get("artist"),
                "our_title": our_track.get("title"),
                "shazam_artist": None,
                "shazam_title": None,
                "reason": "NO SHAZAM MATCH"
            })
            await asyncio.sleep(1.5)
            continue
            
        sh_artist = track.get("subtitle", "")
        sh_title = track.get("title", "")
        sh_artist_norm = normalize(sh_artist)
        sh_title_norm = normalize(sh_title)
        
        # Super fuzzy match (check if there's overlap in words)
        our_title_words = set(our_title_norm.split())
        sh_title_words = set(sh_title_norm.split())
        
        our_artist_words = set(our_artist_norm.split())
        sh_artist_words = set(sh_artist_norm.split())
        
        title_match = (our_title_norm in sh_title_norm or sh_title_norm in our_title_norm 
                      or len(our_title_words & sh_title_words) > 0)
                      
        artist_match = (our_artist_norm in sh_artist_norm or sh_artist_norm in our_artist_norm
                       or len(our_artist_words & sh_artist_words) > 0)
        
        if title_match and artist_match:
            print(f"  ✓ [{i}/{len(files)}] ID {cid}: {our_label} == {sh_artist} - {sh_title}")
        else:
            print(f"  ✗ [{i}/{len(files)}] ID {cid}: MISMATCH!")
            print(f"      Ours:   {our_label}")
            print(f"      Shazam: {sh_artist} - {sh_title}")
            mismatches.append({
                "id": cid,
                "our_artist": our_track.get("artist"),
                "our_title": our_track.get("title"),
                "shazam_artist": sh_artist,
                "shazam_title": sh_title,
                "reason": "METADATA MISMATCH"
            })
            
        # Save incrementally
        with open("/tmp/shazam_mismatches.json", "w") as f:
            json.dump(mismatches, f, indent=2)
                
        await asyncio.sleep(1.5) # Rate limiting

    print(f"\nAudit complete. Found {len(mismatches)} mismatches/unidentified out of {len(files)}.")

asyncio.run(audit())
