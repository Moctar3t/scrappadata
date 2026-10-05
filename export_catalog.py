import os
import json
import re
import subprocess
import hashlib

def write_json_if_changed(filepath, data, **kwargs):
    new_content = json.dumps(data, **kwargs)
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            old_content = f.read()
        if old_content == new_content:
            return False
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)
    return True

VPS_HOST = "204.168.236.140"
APP_DIR = "/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata"
INDEX_PATH = os.path.join(APP_DIR, "music-index.json")
SONGS_DIR = os.path.join(APP_DIR, "songs")
ID_MAP_PATH = os.path.join(os.path.dirname(__file__), "catalog_id_map.json")

def normalize_name(text):
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r'\(feat\..*?\)', '', text)
    text = re.sub(r'\(with.*?\)', '', text)
    text = re.sub(r'ft\..*?$', '', text)
    text = re.sub(r'ft .*?$', '', text)
    text = re.sub(r'feat .*?$', '', text)
    text = re.sub(r'[^a-z0-9]', '', text)
    return text

def merged_artist(t):
    """The app surfaces featured artists by parsing the artist/title text (the ';'
    collaborator convention), NOT a separate `featuring` field — so fold Genius's
    featured artists into the artist string, de-duped, or feats are lost entirely."""
    base = (t.get('artist') or '').strip()
    feat = (t.get('featuring') or '').strip()
    if not feat:
        return base
    have = {a.strip().lower() for a in base.split(';') if a.strip()}
    extra = [f.strip() for f in re.split(r'[;,]', feat) if f.strip() and f.strip().lower() not in have]
    if not extra:
        return base
    return (base + ", " + ", ".join(extra)) if base else ", ".join(extra)


def fetch_ready_tracks():
    # Ready CATALOG tracks only — the owner filter keeps user-upload test tracks
    # (rickrolls, "Test User", etc.) OUT of the catalogue. Without it, every public
    # user upload leaked in as a junk song whose stems 404 (they live under
    # usertracks/, not catalogtracks/).
    query = ("SELECT row_to_json(t) FROM tracks t WHERE status='ready' AND visibility='public' "
             "AND owner_id=(SELECT id FROM users WHERE username='__catalog__')")
    cmd = ["ssh", f"root@{VPS_HOST}", f"sudo -u postgres psql -d zingy -t -A -c \"{query}\""]
    print("Fetching ready tracks from VPS database...")
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    tracks = []
    for line in res.stdout.strip().split('\n'):
        if line:
            tracks.append(json.loads(line))
    return tracks

def main():
    if not os.path.exists(SONGS_DIR):
        os.makedirs(SONGS_DIR)
        
    with open(INDEX_PATH, 'r', encoding='utf-8') as f:
        catalog = json.load(f)
        
    release_formats_path = os.path.join(APP_DIR, "release-formats.json")
    if os.path.exists(release_formats_path):
        try: release_formats = json.load(open(release_formats_path, encoding="utf-8"))
        except Exception: release_formats = {}
    else:
        release_formats = {}
        
    # Build map of existing songs in catalog by normalized name
    existing_by_key = {}
    cleaned_songs_list = []
    removed_duplicates_count = 0
    
    for s in catalog.get("songsList", []):
        key = (normalize_name(s.get("title")), normalize_name(s.get("artist")))
        if key in existing_by_key:
            # We found an existing duplicate in the local music-index! Skip appending it.
            removed_duplicates_count += 1
            # Clean up its song file if it exists
            old_id = s.get("id")
            if old_id:
                padded = old_id.zfill(4) if old_id.isdigit() else old_id
                old_fp = os.path.join(SONGS_DIR, f"{padded}.json")
                if os.path.exists(old_fp):
                    try: os.remove(old_fp)
                    except Exception: pass
            continue
        existing_by_key[key] = s
        cleaned_songs_list.append(s)
        
    catalog["songsList"] = cleaned_songs_list
    # Index catalog entries by their UUID too, so a DB track whose TITLE changed
    # (e.g. after title-cleaning) still updates its existing entry in place instead
    # of being re-added as a duplicate under the new title.
    existing_by_id = {str(s.get("id")): s for s in cleaned_songs_list}
    print(f"Removed {removed_duplicates_count} pre-existing duplicates from music-index.json.")

    # ── NUMERIC catalog ids — so NO app rebuild is ever needed ───────────────
    # The shipped app resolves a detail file as String(format:"%04d", Int(id)) — it
    # ONLY understands numeric ids ("1369" -> songs/1369.json). A UUID id collapses to
    # "0000.json" -> 404 -> the song shows but won't play and has no tags. So every
    # catalogue song gets a STABLE numeric id (persisted by DB UUID in
    # catalog_id_map.json) and its detail file is named numerically. Stems keep their
    # catalogtracks/<uuid>/ URLs (those live inside the JSON, the id doesn't matter).
    id_map = {}
    if os.path.exists(ID_MAP_PATH):
        try: id_map = json.load(open(ID_MAP_PATH, encoding="utf-8"))
        except Exception: id_map = {}
    used = {int(s["id"]) for s in catalog["songsList"] if str(s.get("id", "")).isdigit()}
    used |= {int(v) for v in id_map.values()}
    counter = [max(used) + 1 if used else 1]
    def numeric_id(uuid):
        if uuid in id_map:
            return id_map[uuid]
        while counter[0] in used:
            counter[0] += 1
        id_map[uuid] = str(counter[0]); used.add(counter[0]); counter[0] += 1
        return id_map[uuid]

    tracks = fetch_ready_tracks()
    new_count = 0
    updated_count = 0
    deleted_uuid_files = 0
    modified_files = set()
    
    for t in tracks:
        # Check if song already exists in catalog by normalized key
        key = (normalize_name(t.get('title')), normalize_name(t.get('artist')))
        
        dur_sec = t.get('duration_sec') or 0
        dur_str = f"{dur_sec // 60}:{dur_sec % 60:02d}"
        
        # Stems
        stems = []
        stem_keys = [("bass", "bass_url"), ("drums", "drums_url"), ("other", "other_url"), ("vocals", "vocals_url")]
        for name, url_key in stem_keys:
            url = t.get(url_key)
            if url:
                url = url.replace(".mp3", ".m4a")
                stems.append({
                    "id": name,
                    "name": name.upper(),
                    "url": url
                })
        
        cat_id = numeric_id(str(t['id']))
        s = existing_by_id.get(cat_id) or existing_by_id.get(str(t['id'])) or existing_by_key.get(key)
        
        # Determine release format string. NO DATA ≠ "Single" — that default
        # stamped 2k+ songs as singles before the backfill filled album/release_type.
        # Unknown keeps whatever the map already says (curated) or writes nothing.
        rel_type = t.get('release_type', '')
        album_name = t.get('album', '')
        if rel_type and rel_type.lower() == 'single':
            release_formats[str(cat_id)] = "Single"
        elif rel_type and album_name:
            release_formats[str(cat_id)] = f"{rel_type.capitalize()}: {album_name}"
        elif album_name:
            release_formats[str(cat_id)] = f"Album: {album_name}"

        if s is not None:
            # Update in place — and re-key the entry to its NUMERIC catalog id.
            s["id"] = cat_id
            tid = cat_id

            # Update fields in index entry
            s["title"] = t.get('title', '')
            s["artist"] = merged_artist(t) or s.get("artist") or ""
            s["coverArt"] = t.get('cover_url') or s.get("coverArt") or ""
            # Genius art is primary; the previous (Spotify/Apple) cover rides along
            # as the fallback the app auto-registers (ArtworkFallback).
            if t.get('cover_url_fallback'):
                s["coverArtFallback"] = t['cover_url_fallback']
            s["bpm"] = t.get('bpm') or s.get("bpm") or 0
            s["key"] = t.get('song_key') or s.get("key") or ""
            s["producers"] = t.get('producers', '')
            s["writers"] = t.get('writers', '')
            s["hasStems"] = True
            
            # FILL-only: DB genres (coarse Deezer buckets like "Rap/Hip Hop") must
            # never stomp curated index tags ("drill uk", "grime"). refine_genres.py
            # is the authority that reconciles both directions.
            db_genres = t.get('genres')
            if db_genres and not s.get("genres"):
                s["genres"] = [g.strip() for g in db_genres.split(',') if g.strip()]
            elif s.get("genres") is None:
                s["genres"] = []
            
            # Write updated song detail file (numeric, zero-padded like the app expects)
            padded_id = "%04d" % int(tid)
            song_path = os.path.join(SONGS_DIR, f"{padded_id}.json")
            
            song_entry = {
                "id": tid,
                "title": t.get('title', ''),
                "artist": merged_artist(t),
                "coverArt": t.get('cover_url') or s.get("coverArt") or "",
                "displayDuration": dur_str,
                "duration": dur_sec,
                "bpm": t.get('bpm') or 0,
                "key": t.get('song_key', ''),
                "year": str(t.get('year') or ''),
                "producers": t.get('producers', ''),
                "writers": t.get('writers', ''),
                "recordLabel": t.get('record_label', ''),
                "stems": stems
            }
            if t.get('cover_url_fallback'):
                song_entry["coverArtFallback"] = t['cover_url_fallback']
            if write_json_if_changed(song_path, song_entry, indent=2, ensure_ascii=False):
                modified_files.add(f"songs/{padded_id}.json")

            # If the database UUID was previously exported as a separate file, delete it
            db_uuid = str(t['id'])
            if db_uuid != tid:
                uuid_fp = os.path.join(SONGS_DIR, f"{db_uuid}.json")
                if os.path.exists(uuid_fp):
                    try:
                        os.remove(uuid_fp)
                        deleted_uuid_files += 1
                    except Exception:
                        pass
                # Also remove the UUID entry from songsList if it was added in a previous run
                catalog["songsList"] = [item for item in catalog["songsList"] if item.get("id") != db_uuid]
                        
            updated_count += 1
        else:
            # Add as new track — numeric catalog id so the shipped app can load it.
            tid = cat_id
            db_genres = t.get('genres')
            genres_list = [g.strip() for g in db_genres.split(',') if g.strip()] if db_genres else []
            idx_entry = {
                "id": tid,
                "title": t.get('title', ''),
                "artist": merged_artist(t),
                "coverArt": t.get('cover_url', ''),
                "duration": dur_str,
                "bpm": t.get('bpm') or 0,
                "key": t.get('song_key', ''),
                "producers": t.get('producers', ''),
                "writers": t.get('writers', ''),
                "hasStems": True,
                "genres": genres_list
            }
            if t.get('cover_url_fallback'):
                idx_entry["coverArtFallback"] = t['cover_url_fallback']
            catalog["songsList"].append(idx_entry)

            song_path = os.path.join(SONGS_DIR, f"{'%04d' % int(tid)}.json")
            song_entry = {
                "id": tid,
                "title": t.get('title', ''),
                "artist": merged_artist(t),
                "coverArt": t.get('cover_url', ''),
                "displayDuration": dur_str,
                "duration": dur_sec,
                "bpm": t.get('bpm') or 0,
                "key": t.get('song_key', ''),
                "year": str(t.get('year') or ''),
                "producers": t.get('producers', ''),
                "writers": t.get('writers', ''),
                "recordLabel": t.get('record_label', ''),
                "stems": stems
            }
            if t.get('cover_url_fallback'):
                song_entry["coverArtFallback"] = t['cover_url_fallback']
            if write_json_if_changed(song_path, song_entry, indent=2, ensure_ascii=False):
                modified_files.add(f"songs/{'%04d' % int(tid)}.json")
                
            new_count += 1
            
    if write_json_if_changed(INDEX_PATH, catalog, separators=(',', ':'), ensure_ascii=False):
        modified_files.add("music-index.json")
        
    if write_json_if_changed(release_formats_path, release_formats, indent=2, ensure_ascii=False):
        modified_files.add("release-formats.json")

    # Persist the UUID -> numeric id map so ids stay stable across runs.
    with open(ID_MAP_PATH, 'w', encoding='utf-8') as f:
        json.dump(id_map, f, indent=0, ensure_ascii=False)
        
    print(f"Catalog Export complete!")
    print(f"  Added {new_count} brand new tracks.")
    print(f"  Updated/Deduplicated {updated_count} existing tracks in-place with latest stems/metadata.")
    print(f"  Cleaned up {deleted_uuid_files} redundant UUID song files.")

    # Write delta file
    delta_path = "/tmp/export_changed.txt"
    with open(delta_path, "w", encoding="utf-8") as f:
        for path in modified_files:
            f.write(path + "\n")
    print(f"  Wrote {len(modified_files)} changed files to {delta_path}.")

if __name__ == "__main__":
    main()
