import json
import urllib.request
import urllib.parse
import os
import librosa
import numpy as np

# Krumhansl-Schmuckler key profiles
MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
PITCH_CLASSES = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']

def estimate_key(chroma):
    chroma_vals = np.sum(chroma, axis=1)
    
    maj_corrs = []
    min_corrs = []
    
    for i in range(12):
        shifted_maj = np.roll(MAJOR_PROFILE, i)
        shifted_min = np.roll(MINOR_PROFILE, i)
        
        maj_corrs.append(np.corrcoef(chroma_vals, shifted_maj)[0, 1])
        min_corrs.append(np.corrcoef(chroma_vals, shifted_min)[0, 1])
        
    maj_corrs = np.array(maj_corrs)
    min_corrs = np.array(min_corrs)
    
    best_maj = np.argmax(maj_corrs)
    best_min = np.argmax(min_corrs)
    
    if maj_corrs[best_maj] > min_corrs[best_min]:
        return f"{PITCH_CLASSES[best_maj]} Major"
    else:
        return f"{PITCH_CLASSES[best_min]} Minor"

def fetch_itunes_metadata(query):
    url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&entity=song&limit=1"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            if data['resultCount'] > 0:
                result = data['results'][0]
                art = result.get('artworkUrl100', '').replace('100x100bb', '1000x1000bb')
                year = result.get('releaseDate', '')[:4]
                return art, year
    except Exception as e:
        print(f"Failed to fetch iTunes data for {query}: {e}")
    return "", "Unknown"

def main():
    tracklist_path = "tracklist.txt"
    tracks_dir = "tracks"
    
    song_cards = []
    song_records = []
    
    if not os.path.exists(tracklist_path):
        print(f"Error: {tracklist_path} not found.")
        return
        
    with open(tracklist_path, 'r', encoding='utf-8') as f:
        lines = [l.strip() for l in f if l.strip()]
        
    for i, line in enumerate(lines):
        print(f"Processing ({i+1}/{len(lines)}): {line}")
        parts = line.split(" - ", 1)
        artist = parts[0]
        title = parts[1] if len(parts) > 1 else line
        
        # 1. Fetch iTunes metadata
        cover_art, year = fetch_itunes_metadata(f"{artist} {title}")
        
        # 2. Analyze MP3
        mp3_path = os.path.join(tracks_dir, f"{line}.mp3")
        bpm = 120
        key = "Unknown"
        duration_sec = 240
        duration_str = "4:00"
        
        if os.path.exists(mp3_path):
            try:
                # Load a slice (first 60 seconds) to speed up analysis
                y, sr = librosa.load(mp3_path, duration=60)
                duration_sec = int(librosa.get_duration(path=mp3_path))
                mins = duration_sec // 60
                secs = duration_sec % 60
                duration_str = f"{mins}:{secs:02d}"
                
                # Estimate BPM
                tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
                bpm = int(round(tempo[0] if isinstance(tempo, np.ndarray) else tempo))
                
                # Estimate Key
                chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
                key = estimate_key(chroma)
            except Exception as e:
                print(f"Failed to analyze {mp3_path}: {e}")
        else:
            print(f"File not found: {mp3_path}")
            
        song_id = str(len(song_cards) + 100) # Give them new IDs starting at 100
        
        song_cards.append({
            "id": song_id,
            "title": title,
            "artist": artist,
            "coverArt": cover_art,
            "duration": duration_str
        })
        
        song_records.append({
            "id": song_id,
            "title": title,
            "artist": artist,
            "duration": duration_sec,
            "bpm": bpm,
            "key": key,
            "year": year
        })

    # Save outputs
    with open('metadata_cards.json', 'w', encoding='utf-8') as f:
        json.dump(song_cards, f, indent=2, ensure_ascii=False)
    with open('metadata_records.json', 'w', encoding='utf-8') as f:
        json.dump(song_records, f, indent=2, ensure_ascii=False)
        
    print("Done generating metadata.")

if __name__ == "__main__":
    main()
