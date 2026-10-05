import json, os, urllib.request

with open("candidates.json", "r") as f:
    candidates = json.load(f)

secret = os.environ.get("ZINGY_WORKER_SECRET")
if not secret:
    print("Error: Please set ZINGY_WORKER_SECRET environment variable.")
    exit(1)

api = "https://zingy.dime.systems/api/internal/split/catalog"
sent = 0
skipped = 0

print(f"Enqueuing {len(candidates)} candidates...")
for t in candidates:
    payload = {
        "youtubeUrl": t.get("youtube_url") or t.get("url"),
        "title": t.get("title"),
        "artist": t.get("artist"),
        "coverUrl": t.get("art_url")
    }
    
    req = urllib.request.Request(
        api, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-worker-secret": secret}
    )
    
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.load(r)
            status = res.get("status", "unknown")
            dedup = "(dedup)" if res.get("dedup") else ""
            print(f"  -> {t.get('artist')} - {t.get('title')} [{status}] {dedup}")
            sent += 1
    except Exception as e:
        print(f"  -> {t.get('artist')} - {t.get('title')} [ERROR: {e}]")
        skipped += 1

print(f"Done. Sent {sent}, Skipped {skipped}.")
