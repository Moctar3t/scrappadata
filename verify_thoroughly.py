import re

catalog_path = "/Users/dime/Desktop/AZ-Systems-Hub-2026-04-06/src/data/music-catalog.ts"
filtered_path = "/Users/dime/Desktop/DIME_STEM_PIPELINE.nosync/filtered_spotify_tracklist.txt"

# 1. Parse catalog
catalog_songs = []
with open(catalog_path, "r", encoding="utf-8") as f:
    content = f.read()

# Match the title and artist patterns in the songsList array
card_matches = re.findall(r'title:\s*"([^"]+)",\s*artist:\s*"([^"]+)"', content)
for title, artist in card_matches:
    catalog_songs.append({
        'title': title.strip(),
        'artist': artist.strip(),
        'source': 'songsList'
    })

# Also extract from songsRecord just in case
record_matches = re.findall(r'title:\s*"([^"]+)",\s*artist:\s*"([^"]+)"', content)
for title, artist in record_matches:
    catalog_songs.append({
        'title': title.strip(),
        'artist': artist.strip(),
        'source': 'songsRecord'
    })

# Deduplicate catalog parsed list
unique_catalog = {}
for item in catalog_songs:
    key = (item['title'].lower(), item['artist'].lower())
    unique_catalog[key] = item

print(f"Loaded {len(unique_catalog)} unique catalog entries from music-catalog.ts.")

# 2. Parse filtered tracklist
filtered_songs = []
with open(filtered_path, "r", encoding="utf-8") as f:
    for i, line in enumerate(f):
        line = line.strip()
        if not line or " - " not in line:
            continue
        parts = line.split(" - ", 1)
        artist_name = parts[0].strip()
        track_name = parts[1].strip()
        filtered_songs.append({
            'line_num': i + 1,
            'artist': artist_name,
            'title': track_name
        })

print(f"Loaded {len(filtered_songs)} tracks from filtered_spotify_tracklist.txt.")

# 3. Thorough, loose matching function
def get_words(text):
    # Convert to lowercase, replace punctuation with spaces, split into words
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    return set(w for w in text.split() if len(w) > 1) # only words with len > 1

overlaps = []
for c_key, c_song in unique_catalog.items():
    c_title_words = get_words(c_song['title'])
    c_artist_words = get_words(c_song['artist'])
    
    # Exclude common noise words from matching
    common_words = {'feat', 'with', 'remix', 'the', 'and', 'for', 'of', 'in', 'to', 'a', 'on', 'show'}
    c_title_words -= common_words
    c_artist_words -= common_words
    
    for f_song in filtered_songs:
        f_title_words = get_words(f_song['title']) - common_words
        f_artist_words = get_words(f_song['artist']) - common_words
        
        # Check for matching titles (e.g. if title shares at least 70% of words or is exact)
        title_exact = c_song['title'].lower() == f_song['title'].lower()
        title_overlap = len(c_title_words & f_title_words) >= min(len(c_title_words), len(f_title_words)) if (c_title_words and f_title_words) else False
        
        if title_exact or title_overlap:
            # Check for matching artists (e.g. if they share at least one word)
            artist_exact = c_song['artist'].lower() == f_song['artist'].lower()
            artist_overlap = len(c_artist_words & f_artist_words) > 0 if (c_artist_words and f_artist_words) else False
            
            if artist_exact or artist_overlap:
                overlaps.append({
                    'catalog': f"'{c_song['artist']}' - '{c_song['title']}'",
                    'filtered': f"Line {f_song['line_num']}: '{f_song['artist']}' - '{f_song['title']}'"
                })

print(f"\nFound {len(overlaps)} loose overlaps:")
for item in overlaps:
    print(f"Catalog: {item['catalog']}")
    print(f"Filtered: {item['filtered']}")
    print("-" * 50)
