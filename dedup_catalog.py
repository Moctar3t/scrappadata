#!/usr/bin/env python3
"""(1) Remove the 7 known stemless duplicate twins. (2) Full-catalog duplicate
scan: group songs by normalized title + artist-token overlap, report groups
with >1 entry (flagging which have stems) so the user can decide further merges.
"""
import json, os, glob, re

APP = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
SONGS = os.path.join(APP, "songs")

def ntitle(t): return re.sub(r'[^a-z0-9]', '', (t or '').lower())
def artist_tokens(a):
    a = (a or '').lower()
    a = re.sub(r'\(feat.*?\)|\[feat.*?\]|feat\.?|ft\.?|featuring', '', a)
    return {re.sub(r'[^a-z0-9]', '', p) for p in re.split(r'[;,&]| x | and ', a) if re.sub(r'[^a-z0-9]', '', p)}

def remove_ids(ids):
    idxp = os.path.join(APP, "music-index.json")
    idx = json.load(open(idxp, encoding="utf-8"))
    before = len(idx["songsList"])
    idx["songsList"] = [s for s in idx["songsList"] if str(s.get("id")) not in ids]
    json.dump(idx, open(idxp, "w", encoding="utf-8"), separators=(',', ':'), ensure_ascii=False)
    rfp = os.path.join(APP, "release-formats.json")
    rf = json.load(open(rfp, encoding="utf-8"))
    for i in ids: rf.pop(i, None)
    json.dump(rf, open(rfp, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    removed_files = 0
    for i in ids:
        fp = os.path.join(SONGS, f"{int(i):04d}.json") if i.isdigit() else os.path.join(SONGS, f"{i}.json")
        if os.path.exists(fp):
            os.remove(fp); removed_files += 1
    return before - len(idx["songsList"]), removed_files

# ---- 1. remove the 7 stemless twins ----
cls = json.load(open("nostem_classified.json", encoding="utf-8"))
twin_ids = [d["id"] for d in cls["dup_of_stemmed"]]
ridx, rfiles = remove_ids(twin_ids)
print(f"Removed {len(twin_ids)} stemless twins: {twin_ids}  (index -{ridx}, files -{rfiles})")

# ---- 2. full duplicate scan on remaining ----
songs = []
for fp in glob.glob(os.path.join(SONGS, "*.json")):
    s = json.load(open(fp, encoding="utf-8"))
    songs.append({"id": str(s.get("id")), "title": s.get("title", ""), "artist": s.get("artist", ""),
                  "nt": ntitle(s.get("title", "")), "at": artist_tokens(s.get("artist", "")),
                  "stems": len(s.get("stems") or [])})

by_title = {}
for s in songs:
    if s["nt"]:
        by_title.setdefault(s["nt"], []).append(s)

groups = []
for nt, grp in by_title.items():
    if len(grp) < 2:
        continue
    # union-find by artist-token overlap (same-title songs with shared artist)
    parent = list(range(len(grp)))
    def find(i):
        while parent[i] != i: parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for i in range(len(grp)):
        for j in range(i + 1, len(grp)):
            if not grp[i]["at"] or not grp[j]["at"] or (grp[i]["at"] & grp[j]["at"]):
                parent[find(i)] = find(j)
    comps = {}
    for i, s in enumerate(grp):
        comps.setdefault(find(i), []).append(s)
    for comp in comps.values():
        if len(comp) >= 2:
            groups.append(comp)

groups.sort(key=lambda g: (-len(g), g[0]["title"].lower()))
report = []
for g in groups:
    report.append([{"id": s["id"], "title": s["title"], "artist": s["artist"], "stems": s["stems"]} for s in g])
json.dump(report, open("duplicate_groups.json", "w"), indent=2, ensure_ascii=False)

dup_songs = sum(len(g) for g in groups)
print(f"\nDUPLICATE GROUPS: {len(groups)} groups covering {dup_songs} songs "
      f"({dup_songs - len(groups)} are redundant copies)")
print("\nTop 25 groups:")
for g in groups[:25]:
    ids = ", ".join(f"{s['id']}({s['stems']}st)" for s in g)
    print(f"  '{g[0]['title'][:32]:32}' [{g[0]['artist'][:22]:22}]  ->  {ids}")
print("\nWrote duplicate_groups.json")
