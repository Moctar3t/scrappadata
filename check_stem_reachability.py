#!/usr/bin/env python3
"""Live reachability check of every stem URL shipped in the app. Read-only.

Uses a ranged GET (bytes=0-0) which BunnyCDN honours even when HEAD is flaky.
Writes stem_reachability.json (per-song status) and prints a summary.
"""
import json, os, glob, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

SONGS = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata/songs"
IDMAP = "/Users/dime/Developer/scrappadata/catalog_id_map.json"
db_numeric = set(str(v) for v in json.load(open(IDMAP)).values())
UA = {"User-Agent": "Mozilla/5.0", "Range": "bytes=0-0"}

def check(url):
    try:
        req = urllib.request.Request(url, headers=UA, method="GET")
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status  # 200 or 206
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return f"ERR:{type(e).__name__}"

# gather all (song_id, title, artist, db, stem_id, url)
jobs = []
songs = {}
for fp in sorted(glob.glob(os.path.join(SONGS, "*.json"))):
    s = json.load(open(fp, encoding="utf-8"))
    sid = str(s.get("id", ""))
    songs[sid] = {"title": s.get("title", ""), "artist": s.get("artist", ""),
                  "db": sid in db_numeric, "stems": {}, "nostem": not s.get("stems")}
    for st in (s.get("stems") or []):
        u = st.get("url")
        if isinstance(u, str) and u:
            jobs.append((sid, st.get("id", "?"), u))
        else:
            songs[sid]["stems"][st.get("id", "?")] = "BAD_URL_TYPE"

print(f"Checking {len(jobs)} stem URLs across {len(songs)} songs...")
results = {}
with ThreadPoolExecutor(max_workers=40) as ex:
    futs = {ex.submit(check, u): (sid, stid, u) for sid, stid, u in jobs}
    done = 0
    for fut in as_completed(futs):
        sid, stid, u = futs[fut]
        status = fut.result()
        songs[sid]["stems"][stid] = status
        done += 1
        if done % 500 == 0:
            print(f"  ...{done}/{len(jobs)}")

# classify each song
ok = bad = nostem = partial = 0
broken_songs = []
for sid, d in songs.items():
    if d["nostem"]:
        nostem += 1
        continue
    statuses = list(d["stems"].values())
    good = [x for x in statuses if x in (200, 206)]
    if len(good) == len(statuses) and statuses:
        ok += 1
    elif not good:
        bad += 1
        broken_songs.append((sid, d))
    else:
        partial += 1
        broken_songs.append((sid, d))

json.dump(songs, open("stem_reachability.json", "w"), indent=2, ensure_ascii=False)

print("\n===== STEM REACHABILITY =====")
print(f"  fully reachable : {ok}")
print(f"  fully DEAD      : {bad}")
print(f"  partially dead  : {partial}")
print(f"  no stems at all : {nostem}")
print()
db_broken = [(s, d) for s, d in broken_songs if d["db"]]
lg_broken = [(s, d) for s, d in broken_songs if not d["db"]]
print(f"  broken db-backed: {len(db_broken)}   broken legacy: {len(lg_broken)}")
print("\nFirst 25 broken/partial songs:")
for sid, d in broken_songs[:25]:
    print(f"   {sid:>5} {'DB' if d['db'] else 'LG'} | {d['artist'][:24]:24} | {d['title'][:28]:28} | {d['stems']}")
print("\nWrote stem_reachability.json")
