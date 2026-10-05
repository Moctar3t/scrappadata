#!/usr/bin/env python3
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import subprocess

VPS_HOST = "204.168.236.140"
UA = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

def clean_query_params(title, artist):
    # 1. Clean artist: split by semicolon and take the first primary artist
    clean_artist = artist.split(';')[0].strip()
    # If the artist name has features like "feat", "ft", etc., split them
    for marker in [" feat", " ft", " featuring", " (with", " & ", " and "]:
        idx = clean_artist.lower().find(marker)
        if idx != -1:
            clean_artist = clean_artist[:idx].strip()
    
    # 2. Clean title: remove featuring clauses, social handles (@...), video markers, etc.
    clean_title = title
    
    # Remove things like "Ft. @GAZO & @larvfleuze" or "@LAFAMAXOFFICIEL"
    # Remove @ social handles and anything after them in that word/clause
    clean_title = re.sub(r'@[A-Za-z0-9_]+', '', clean_title)
    
    # Remove parentheses/brackets featuring info
    clean_title = re.sub(r'\(feat\..*?\)', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\(ft\..*?\)', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\(with.*?\)', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\[feat\..*?\]', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\[ft\..*?\]', '', clean_title, flags=re.IGNORECASE)
    
    # Remove free-standing "feat. ...", "ft. ...", etc.
    clean_title = re.sub(r'\b(feat|ft|featuring|with)\b.*$', '', clean_title, flags=re.IGNORECASE)
    
    # Remove "DJ ..." if it appears later in the title (collaboration / remix annotation)
    dj_idx = clean_title.lower().find(' dj ')
    if dj_idx != -1:
        clean_title = clean_title[:dj_idx].strip()
    dj_idx_start = clean_title.lower().find('dj ')
    if dj_idx_start > 2:
        clean_title = clean_title[:dj_idx_start].strip()

    # Remove other junk like "M/V", "Official Video", etc.
    clean_title = re.sub(r'\b(M/V|Official Video|Official Audio|Clip Officiel|Lyric Video|Audio)\b', '', clean_title, flags=re.IGNORECASE)
    # Remove year patterns in parentheses like (2001)
    clean_title = re.sub(r'\(\d{4}\)', '', clean_title)
    # Remove album annotations like "// Album gratuit ..."
    clean_title = clean_title.split('//')[0].strip()
    
    # Clean up excess whitespace and punctuation
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    clean_title = re.sub(r'[-–—/]+$', '', clean_title).strip() # trailing dash/slash
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
            # Try a second fallback with just the title
            query_title_only = clean_title.strip()
            search_url_fallback = f'https://genius.com/api/search/multi?q={urllib.parse.quote(query_title_only)}'
            data_fallback = fetch_from_url(search_url_fallback)
            for section in data_fallback.get('response', {}).get('sections', []):
                if section.get('type') == 'song' and section.get('hits'):
                    # Check if the primary artist matches or contains parts of our artist
                    for hit in section['hits']:
                        hit_artist = hit['result'].get('primary_artist', {}).get('name', '').lower()
                        if clean_artist.lower() in hit_artist or hit_artist in clean_artist.lower():
                            song_id = hit['result'].get('id')
                            break
                    if song_id:
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
            # Fallback search
            q_fallback = f'{clean_artist} {clean_title}'
            url = f'https://api.deezer.com/search?q={urllib.parse.quote(q_fallback)}&limit=5'
            data = fetch_from_url(url)
            tracks = data.get('data', [])
            
        if not tracks:
            return None
            
        # Match best track
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
            "release_date": release_date
        }
    except Exception as e:
        print(f"Deezer scrape failed for '{artist} - {title}': {e}")
        return None

def escape_sql_string(val):
    if val is None:
        return "NULL"
    val_str = str(val).replace("'", "''")
    return f"'{val_str}'"

def escape_sql_int(val):
    if val is None:
        return "NULL"
    try:
        return str(int(val))
    except (ValueError, TypeError):
        return "NULL"

def main():
    # Fetch tracks from VPS that have NULL producers, record_label, or year
    query = "SELECT row_to_json(r) FROM (SELECT id, title, artist, producers, record_label, year, release_date, featuring FROM tracks WHERE status='ready' AND (producers IS NULL OR record_label IS NULL OR year IS NULL)) r;"
    cmd = ["ssh", f"root@{VPS_HOST}", f"sudo -u postgres psql -d zingy -t -A -c \"{query}\""]
    print("Fetching tracks with missing metadata from VPS...")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except Exception as e:
        print(f"Failed to fetch tracks: {e}")
        return

    lines = res.stdout.strip().split('\n')
    tracks = []
    for line in lines:
        if line.strip():
            try:
                tracks.append(json.loads(line))
            except Exception:
                pass

    print(f"Found {len(tracks)} tracks with missing metadata to enrich.")
    if not tracks:
        print("All tracks fully enriched!")
        return

    sql_statements = []
    success_count = 0

    for i, t in enumerate(tracks):
        tid = t['id']
        title = t['title']
        artist = t['artist']
        
        db_producers = t.get('producers')
        db_label = t.get('record_label')
        db_year = t.get('year')
        db_rdate = t.get('release_date')
        db_featuring = t.get('featuring')
        
        # Clean title & artist for console output
        ct, ca = clean_query_params(title, artist)
        print(f"[{i+1}/{len(tracks)}] Scraping: {artist} - {title} (Search query: {ca} - {ct})")
        
        # Scrape Genius & Deezer
        genius_data = scrape_genius(title, artist)
        deezer_data = scrape_deezer(title, artist)
        
        # Merge values: only overwrite if DB has NULL/empty
        producers = db_producers
        featuring = db_featuring
        release_date = db_rdate
        label = db_label
        
        if genius_data:
            if not producers:
                producers = genius_data.get('producers')
            if not featuring:
                featuring = genius_data.get('featuring')
            if not release_date:
                release_date = genius_data.get('release_date')
                
        if deezer_data:
            if not label:
                label = deezer_data.get('label')
            if not release_date:
                release_date = deezer_data.get('release_date')
                
        # Extract year
        year = db_year
        if not year and release_date:
            m = re.match(r'^(\d{4})', str(release_date))
            if m:
                year = int(m.group(1))

        # Format update query
        prod_esc = escape_sql_string(producers)
        feat_esc = escape_sql_string(featuring)
        year_esc = escape_sql_int(year)
        label_esc = escape_sql_string(label)
        rdate_esc = escape_sql_string(release_date)
        
        # If we successfully scraped something new, or at least performed the scraping request
        if genius_data is not None or deezer_data is not None:
            sql = f"UPDATE tracks SET producers = {prod_esc}, featuring = {feat_esc}, year = {year_esc}, record_label = {label_esc}, release_date = {rdate_esc}, meta_analyzed = true WHERE id = '{tid}';"
            sql_statements.append(sql)
            success_count += 1
            print(f" -> Updated: Prod: {producers}, Feat: {featuring}, Year: {year}, Label: {label}")
        else:
            print(" -> Temporary API failure or no response. Skipping update for retry later.")
            
        time.sleep(0.5) # Be polite

    if sql_statements:
        print(f"\nSending {len(sql_statements)} updates to VPS database...")
        sql_batch = "\n".join(sql_statements)
        
        # Write to temporary local file
        tmp_file = "/tmp/metadata_updates.sql"
        with open(tmp_file, "w") as f:
            f.write(sql_batch)
            
        # Pipe to psql via ssh
        pipe_cmd = f"cat {tmp_file} | ssh root@{VPS_HOST} \"sudo -u postgres psql -d zingy\""
        try:
            subprocess.run(pipe_cmd, shell=True, check=True)
            print("Successfully updated database!")
        except Exception as e:
            print(f"Failed to execute SQL updates: {e}")
        finally:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
    else:
        print("\nNo updates to perform.")

if __name__ == '__main__':
    main()
