import os
import re
import time
import json
import urllib.request
import urllib.parse
import psycopg2
from psycopg2.extras import DictCursor

DB_DSN = "postgresql://zingy_user:a6669ec7d821a6b5efa0088ca2b7aa5516dc89b8c8de6884@127.0.0.1:5432/zingy"

UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

def clean_query_params(title, artist):
    if not artist: artist = ""
    if not title: title = ""
    clean_artist = artist.split(';')[0].strip()
    for marker in [" feat", " ft", " featuring", " (with", " & ", " and "]:
        idx = clean_artist.lower().find(marker)
        if idx != -1:
            clean_artist = clean_artist[:idx].strip()
    
    clean_title = title
    clean_title = re.sub(r'@[A-Za-z0-9_]+', '', clean_title)
    clean_title = re.sub(r'\(feat\..*?\)', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\(ft\..*?\)', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\(with.*?\)', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\[feat\..*?\]', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\[ft\..*?\]', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\b(feat|ft|featuring|with)\b.*$', '', clean_title, flags=re.IGNORECASE)
    
    dj_idx = clean_title.lower().find(' dj ')
    if dj_idx != -1:
        clean_title = clean_title[:dj_idx].strip()
    dj_idx_start = clean_title.lower().find('dj ')
    if dj_idx_start > 2:
        clean_title = clean_title[:dj_idx_start].strip()

    clean_title = re.sub(r'\b(M/V|Official Video|Official Audio|Clip Officiel|Lyric Video|Audio)\b', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\(\d{4}\)', '', clean_title)
    clean_title = clean_title.split('//')[0].strip()
    
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    clean_title = re.sub(r'[-–—/]+$', '', clean_title).strip()
    clean_title = clean_title.strip()
    
    return clean_title, clean_artist

def fetch_from_url(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode('utf-8'))

def scrape_genius(title, artist):
    clean_title, clean_artist = clean_query_params(title, artist)
    query = f"{clean_artist} {clean_title}".strip()
    search_url = f'https://genius.com/api/search/multi?q={urllib.parse.quote(query)}'
    
    try:
        data = fetch_from_url(search_url)
        song_id = None
        for section in data.get('response', {}).get('sections', []):
            if section.get('type') == 'song' and section.get('hits'):
                song_id = section['hits'][0]['result'].get('id')
                break
                
        if not song_id:
            return None
            
        time.sleep(0.1)
        song_url = f'https://genius.com/api/songs/{song_id}'
        data = fetch_from_url(song_url)
        song = data.get('response', {}).get('song', {})
        
        producers = [p.get('name') for p in song.get('producer_artists', [])]
        
        # supplement from production-role custom_performances
        prod_roles = {"producer", "co-producer", "additional production", "additional producer"}
        for perf in song.get('custom_performances', []):
            if perf.get('label', '').strip().lower() in prod_roles:
                producers += [a['name'] for a in perf.get('artists', [])]
                
        # deduplicate producers
        seen = set()
        producers_clean = []
        for p in producers:
            k = p.strip().lower()
            if k and k not in seen and k not in {"self-produced", "n/a", "none"}:
                seen.add(k)
                producers_clean.append(p.strip())
                
        features = [f.get('name') for f in song.get('featured_artists', [])]
        release_date = song.get('release_date')
        
        return {
            "producers": ", ".join(producers_clean) if producers_clean else None,
            "featuring": ", ".join(features) if features else None,
            "release_date": release_date
        }
    except Exception as e:
        print(f"Genius scrape failed for '{query}': {e}")
        return None

def scrape_deezer(title, artist):
    clean_title, clean_artist = clean_query_params(title, artist)
            
    q = f'track:"{clean_title}" artist:"{clean_artist}"'
    url = f'https://api.deezer.com/search?q={urllib.parse.quote(q)}&limit=5'
    
    try:
        data = fetch_from_url(url)
        tracks = data.get('data', [])
        if not tracks:
            q_fallback = f'{clean_artist} {clean_title}'
            url = f'https://api.deezer.com/search?q={urllib.parse.quote(q_fallback)}&limit=5'
            data = fetch_from_url(url)
            tracks = data.get('data', [])
            
        if not tracks:
            return None
            
        best_track = tracks[0]
        album_id = best_track.get('album', {}).get('id')
        if not album_id:
            return None
            
        time.sleep(0.1)
        album_url = f'https://api.deezer.com/album/{album_id}'
        album_data = fetch_from_url(album_url)
        
        label = album_data.get('label')
        release_date = album_data.get('release_date')
        
        return {
            "label": label.strip() if label else None,
            "release_date": release_date,
            "album": album_data.get('title'),
            "release_type": album_data.get('record_type')
        }
    except Exception as e:
        print(f"Deezer scrape failed for '{artist} - {title}': {e}")
        return None

def process_unanalyzed_tracks():
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("SELECT id, title, artist FROM tracks WHERE meta_analyzed = false AND title IS NOT NULL LIMIT 20;")
        tracks = cur.fetchall()
        
        if not tracks:
            return 0
            
        for t in tracks:
            print(f"Scraping metadata for: {t['artist']} - {t['title']}")
            
            genius_data = scrape_genius(t['title'], t['artist'])
            deezer_data = scrape_deezer(t['title'], t['artist'])
            
            producers = None
            featuring = None
            release_date = None
            label = None
            album = None
            release_type = None
            
            if genius_data:
                producers = genius_data.get('producers')
                featuring = genius_data.get('featuring')
                release_date = genius_data.get('release_date')
                
            if deezer_data:
                label = deezer_data.get('label')
                album = deezer_data.get('album')
                release_type = deezer_data.get('release_type')
                if not release_date:
                    release_date = deezer_data.get('release_date')
                    
            year = None
            if release_date:
                m = re.match(r'^(\d{4})', str(release_date))
                if m:
                    year = int(m.group(1))
            
            if genius_data is not None or deezer_data is not None:
                cur.execute("""
                    UPDATE tracks 
                    SET producers = %s, featuring = %s, year = %s, record_label = %s, release_date = %s, album = %s, release_type = %s, meta_analyzed = true 
                    WHERE id = %s
                """, (producers, featuring, year, label, release_date, album, release_type, t['id']))
                print(f" -> Success! Prod: {producers}, Feat: {featuring}, Album: {album} ({release_type})")
            else:
                cur.execute("UPDATE tracks SET meta_analyzed = true WHERE id = %s", (t['id'],))
                print(" -> No match found.")
                
            time.sleep(1)
            
    conn.close()
    return len(tracks)

def main():
    print("Starting Genius+Deezer Metadata Worker Daemon...")
    while True:
        try:
            processed = process_unanalyzed_tracks()
            if processed == 0:
                time.sleep(10)
        except Exception as e:
            print(f"Worker crashed: {e}")
            time.sleep(10)

if __name__ == '__main__':
    main()
