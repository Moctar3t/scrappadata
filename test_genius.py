import urllib.request
import urllib.parse
import json

def search_genius(query):
    url = f'https://genius.com/api/search/multi?q={urllib.parse.quote(query)}'
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            sections = data.get('response', {}).get('sections', [])
            for section in sections:
                if section.get('type') == 'song':
                    hits = section.get('hits', [])
                    if hits:
                        first_hit = hits[0].get('result', {})
                        title = first_hit.get('title')
                        artist = first_hit.get('primary_artist', {}).get('name')
                        song_url = first_hit.get('url')
                        print(f"Match: {artist} - {title} -> {song_url}")
                        return song_url
            return "No match"
    except Exception as e:
        return f"Error: {e}"

search_genius("Inner City - Good Life")
