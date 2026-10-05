import urllib.request
import urllib.parse
import json

def test_fields(query):
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
                        print(json.dumps(first_hit, indent=2))
                        return
    except Exception as e:
        print(f"Error: {e}")

test_fields("Inner City - Good Life")
