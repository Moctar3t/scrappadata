import sys
import urllib.request
import os
import tempfile
import json

try:
    import librosa
    import numpy as np
except ImportError:
    print(json.dumps({"error": "librosa or numpy not installed"}))
    sys.exit(1)

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

def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Missing args: id url"}))
        return
        
    track_id = sys.argv[1]
    url = sys.argv[2]
    
    # Download file to temp path
    fd, temp_path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response, open(temp_path, 'wb') as out_file:
            out_file.write(response.read())
            
        y, sr = librosa.load(temp_path, duration=60)
        duration_sec = int(librosa.get_duration(path=temp_path))
        
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = int(round(tempo[0] if isinstance(tempo, np.ndarray) else tempo))
        
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        key = estimate_key(chroma)
        
        print(json.dumps({
            "id": track_id,
            "bpm": bpm,
            "key": key,
            "duration_sec": duration_sec
        }))
        
    except Exception as e:
        print(json.dumps({"error": str(e)}))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

if __name__ == "__main__":
    main()
