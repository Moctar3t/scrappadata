import os
import yt_dlp
import concurrent.futures
from threading import Lock

# Paths
tracklist_path = "filtered_spotify_tracklist.txt"
tracks_dir = "tracks"

# Create directories
if not os.path.exists(tracks_dir):
    os.makedirs(tracks_dir)

# Load existing tracks
existing_files = set(os.listdir(tracks_dir))

# Parse pre-filtered tracklist
tracks = []
if os.path.exists(tracklist_path):
    with open(tracklist_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or " - " not in line:
                continue
            parts = line.split(" - ", 1)
            artist_name = parts[0].strip()
            track_name = parts[1].strip()
            tracks.append((artist_name, track_name))
else:
    print(f"Error: {tracklist_path} not found!")
    exit(1)

print(f"Loaded {len(tracks)} filtered tracks from {tracklist_path}.")

# Filtering keywords to avoid covers, remixes, and low-quality fan uploads
BAD_KEYWORDS = [
    'cover', 'tribute', 'karaoke', 'instrumental', 'guitar cover', 'drum cover', 
    'synthesizer cover', 'piano cover', 'synthesia', 'fanmade', 'remake'
]

# Thread safety locks
print_lock = Lock()

def safe_print(msg):
    with print_lock:
        print(msg)

def find_best_video_id(artist, title):
    track_query = f"{artist} - {title}"
    
    # Try multiple search strategies in order of reliability
    queries = [
        f"{artist} - {title} \"Provided to YouTube\"",
        f"{artist} - {title} \"official audio\"",
        f"{artist} - {title}"
    ]
    
    ydl_opts_search = {
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
        'no_color': True,
    }
    
    for query in queries:
        try:
            with yt_dlp.YoutubeDL(ydl_opts_search) as ydl:
                result = ydl.extract_info(f"ytsearch5:{query}", download=False)
                if 'entries' in result and result['entries']:
                    for entry in result['entries']:
                        v_title = entry.get('title', '').lower()
                        v_id = entry.get('id')
                        
                        # Exclude any cover/tribute/fake tracks
                        if any(bad in v_title for bad in BAD_KEYWORDS):
                            continue
                            
                        # If we found a valid video ID, return it immediately
                        if v_id:
                            return v_id
        except Exception as e:
            continue
            
    # Absolute fallback: first result of the simplest search
    try:
        with yt_dlp.YoutubeDL(ydl_opts_search) as ydl:
            result = ydl.extract_info(f"ytsearch1:{track_query}", download=False)
            if 'entries' in result and result['entries']:
                return result['entries'][0].get('id')
    except:
        pass
        
    return None

def download_track(index, artist, title):
    track_name_formatted = f"{artist} - {title}"
    filename = f"{track_name_formatted}.mp3"
    
    # Quick sanitize for Windows compatibility (replace invalid characters)
    for c in '<>:"/\\|?*':
        filename = filename.replace(c, "_")
        
    target_path = os.path.join(tracks_dir, filename)
    
    if os.path.exists(target_path):
        safe_print(f"[{index}/{len(tracks)}] Already exists: {filename}")
        return True
        
    safe_print(f"[{index}/{len(tracks)}] Searching for foolproof download for: {track_name_formatted}")
    video_id = find_best_video_id(artist, title)
    
    if not video_id:
        safe_print(f"❌ [{index}/{len(tracks)}] Could not find any reliable matching video for: {track_name_formatted}")
        return False
        
    safe_print(f"✅ [{index}/{len(tracks)}] Found official video ID: {video_id}. Downloading...")
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': True,
        'no_warnings': True,
        'outtmpl': os.path.join(tracks_dir, f"{track_name_formatted}.%(ext)s"),
        'extractor_args': {
            'youtube': {
                'player_client': ['android'],
            }
        }
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        safe_print(f"🎉 [{index}/{len(tracks)}] Successfully downloaded: {track_name_formatted}")
        return True
    except Exception as e:
        safe_print(f"❌ [{index}/{len(tracks)}] Error downloading {track_name_formatted}: {e}")
        return False

def main():
    # Use ThreadPoolExecutor to download 4 tracks in parallel for optimal speed
    # without getting rate-limited
    num_threads = 4
    safe_print(f"Starting foolproof download of {len(tracks)} tracks in parallel (using {num_threads} threads)...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = []
        for i, (artist, title) in enumerate(tracks):
            futures.append(executor.submit(download_track, i + 1, artist, title))
            
        # Wait for all to complete
        concurrent.futures.wait(futures)
        
    safe_print("All parallel downloads finished!")

if __name__ == "__main__":
    main()
