import urllib.request
import json

def test_song_api(song_id):
    url = f'https://genius.com/api/songs/{song_id}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            song = data.get('response', {}).get('song', {})
            # Look at producer relationships
            producers = song.get('producer_artists', [])
            prod_names = [p.get('name') for p in producers]
            print("Producers found:", prod_names)
    except Exception as e:
        print(f"Error: {e}")

test_song_api(468847)
