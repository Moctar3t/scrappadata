import os
import json
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

SONGS_DIR = "/Users/dime/Desktop/AZ-Systems-Hub-2026-04-06/src/data/songs"

def test_url(url):
    try:
        req = urllib.request.Request(
            url, 
            method='HEAD',
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=5.0) as response:
            return response.status == 200 or response.status == 206
    except Exception:
        return False

def fix_mangled_string(s):
    try:
        # 1. CP850/Latin-1 restoration
        s_fixed = s.encode('latin-1').decode('cp850')
        # 2. Straight to curly apostrophe conversion
        s_fixed = s_fixed.replace("'", "’")
        return s_fixed
    except Exception:
        s_fixed = s.replace("'", "’")
        return s_fixed

def process_song(filename):
    filepath = os.path.join(SONGS_DIR, filename)
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            song = json.load(f)
            
        stems = song.get('stems', [])
        if not stems:
            return None
            
        # First, check if the first stem URL is already working.
        # If it is working, we don't need to do anything.
        first_stem_url = stems[0].get('url', '')
        if isinstance(first_stem_url, list):
            first_stem_url = first_stem_url[0]
            
        if first_stem_url and test_url(first_stem_url):
            return None # Already working!
            
        # Not working! Let's apply our intelligent fix.
        updated = False
        new_stems = []
        
        for stem in stems:
            url_val = stem.get('url', '')
            if not url_val:
                new_stems.append(stem)
                continue
                
            urls_list = url_val if isinstance(url_val, list) else [url_val]
            corrected_urls = []
            
            for url in urls_list:
                parsed = urllib.parse.urlparse(url)
                path = urllib.parse.unquote(parsed.path)
                
                parts = path.strip('/').split('/')
                fixed_parts = []
                for p in parts:
                    fixed_parts.append(fix_mangled_string(p))
                
                fixed_path = '/' + '/'.join(fixed_parts)
                quoted_path = urllib.parse.quote(fixed_path)
                
                new_url = f"{parsed.scheme}://{parsed.netloc}{quoted_path}"
                corrected_urls.append(new_url)
                updated = True
                
            new_stem = stem.copy()
            new_stem['url'] = corrected_urls if isinstance(url_val, list) else corrected_urls[0]
            new_stems.append(new_stem)
            
        if updated:
            # Verify the corrected URL
            test_target = new_stems[0]['url']
            if isinstance(test_target, list):
                test_target = test_target[0]
                
            if test_url(test_target):
                song['stems'] = new_stems
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(song, f, indent=2)
                return filename, song.get('title'), f"Fixed & Verified 200 OK -> {test_target}"
            else:
                return filename, song.get('title'), f"FAILED verification: {test_target}"
                
    except Exception as e:
        return filename, None, f"Error: {e}"
        
    return None

def main():
    print("=== Self-Healing Mangled Stem URLs Fixer ===")
    song_files = [f for f in os.listdir(SONGS_DIR) if f.endswith(".json")]
    
    results = []
    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(process_song, fn): fn for fn in song_files}
        for future in as_completed(futures):
            res = future.result()
            if res:
                results.append(res)
                
    print(f"\nProcessed {len(song_files)} song files.")
    updates = [r for r in results if "Fixed" in r[2]]
    failures = [r for r in results if "FAILED" in r[2]]
    errors = [r for r in results if "Error" in r[2]]
    
    print(f"Successfully fixed & updated: {len(updates)}")
    print(f"Failed verification: {len(failures)}")
    print(f"Errors: {len(errors)}")
    
    if updates:
        print("\nSuccessful Self-Healing Updates:")
        for fn, title, msg in sorted(updates):
            print(f" - {fn} ({title}): {msg}")
            
    if failures:
        print("\nFailed Verifications (still 404):")
        for fn, title, msg in sorted(failures):
            print(f" - {fn} ({title}): {msg}")
            
    if errors:
        print("\nErrors:")
        for fn, title, msg in sorted(errors):
            print(f" - {fn} ({title}): {msg}")

if __name__ == "__main__":
    main()
