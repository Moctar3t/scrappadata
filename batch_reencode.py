"""
batch_reencode.py
-----------------
1. Unzips all ZIPs from C:/Users/Dimé/Desktop/catana
2. Converts every MP3 -> AAC (.m4a) with ffmpeg in parallel
3. Uploads each .m4a to BunnyCDN at the SAME path as the source MP3
   e.g.  SongFolder/bass.mp3  ->  SongFolder/bass.m4a  on CDN
"""

import os
import sys
import json
import zipfile
import subprocess
import time
import urllib.request
import urllib.parse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── CONFIG ─────────────────────────────────────────────────────────────────────
STORAGE_ZONE  = "videobb"
# The storage zone key comes from the environment, never from this file.
ACCESS_KEY = os.environ.get("BUNNY_STORAGE_PASSWORD") or sys.exit("set BUNNY_STORAGE_PASSWORD")
CDN_STORAGE   = f"https://storage.bunnycdn.com/{STORAGE_ZONE}/"

ZIP_SOURCE    = Path(r"C:\Users\Dimé\Desktop\catana")
WORK_DIR      = Path(r"C:\Users\Dimé\Desktop\catana_work")
PROGRESS_FILE = WORK_DIR / "progress.json"

CONCURRENT    = 32      # parallel workers
FFMPEG_BIT    = "128k"  # AAC bitrate
# ───────────────────────────────────────────────────────────────────────────────

# Keep PC awake
try:
    import ctypes
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    print("✓ Keep-awake set")
except Exception as e:
    print(f"Warning: keep-awake failed: {e}")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

progress_lock = threading.Lock()

def load_progress():
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_progress(prog, key):
    with progress_lock:
        prog[key] = "done"
        try:
            with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
                json.dump(prog, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

# ── STEP 1: UNZIP ──────────────────────────────────────────────────────────────
def unzip_all():
    zips = sorted(ZIP_SOURCE.glob("*.zip"))
    if not zips:
        print(f"No ZIP files found in {ZIP_SOURCE}!")
        sys.exit(1)
    print(f"\n📦 Found {len(zips)} ZIP file(s) — extracting...")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    for zp in zips:
        print(f"  {zp.name}  ({zp.stat().st_size / 1e9:.1f} GB)")
        try:
            with zipfile.ZipFile(zp, "r") as z:
                z.extractall(WORK_DIR)
        except Exception as e:
            print(f"  ⚠ Error extracting {zp.name}: {e}")
    print("✓ All ZIPs extracted\n")

# ── STEP 2+3: CONVERT + UPLOAD ─────────────────────────────────────────────────
def process_mp3(mp3_path: Path, prog: dict) -> tuple:
    """
    Convert one MP3 -> M4A, upload to CDN at same relative path.
    CDN path = <parent_folder_name>/<stem>.m4a
    Returns (success, mp3_path, msg)
    """
    key = str(mp3_path)
    if prog.get(key) == "done":
        return True, mp3_path, "skipped"

    song_folder = mp3_path.parent.name   # e.g. "Travis Scott - SICKO MODE"
    m4a_name    = mp3_path.stem + ".m4a" # e.g. "bass.m4a"
    m4a_path    = mp3_path.with_suffix(".m4a")

    # -- Convert MP3 -> AAC --
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3_path),
             "-c:a", "aac", "-b:a", FFMPEG_BIT, str(m4a_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
        )
    except subprocess.CalledProcessError as e:
        return False, mp3_path, f"ffmpeg failed: {e}"

    # -- Upload to CDN: SongFolder/bass.m4a --
    cdn_path   = f"{song_folder}/{m4a_name}"
    upload_url = CDN_STORAGE + urllib.parse.quote(cdn_path, safe="/")

    try:
        with open(m4a_path, "rb") as f:
            data = f.read()
        req = urllib.request.Request(
            upload_url, data=data, method="PUT",
            headers={"AccessKey": ACCESS_KEY, "Content-Type": "application/octet-stream"}
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    if resp.status in (200, 201):
                        break
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
    except Exception as e:
        return False, mp3_path, f"upload failed: {e}"

    # Clean up local .m4a to save disk space
    try:
        m4a_path.unlink()
    except Exception:
        pass

    save_progress(prog, key)
    return True, mp3_path, "done"

# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  Dimé Stems — Batch AAC Re-encoder")
    print("=" * 60)

    unzip_all()

    mp3s = list(WORK_DIR.rglob("*.mp3"))
    if not mp3s:
        print("No MP3 files found after extraction!")
        sys.exit(1)

    prog      = load_progress()
    remaining = [p for p in mp3s if prog.get(str(p)) != "done"]

    print(f"🎵 {len(mp3s)} MP3s total")
    print(f"   Already done : {len(mp3s) - len(remaining)}")
    print(f"   To process   : {len(remaining)}")
    print(f"   Concurrency  : {CONCURRENT} workers")
    print(f"   Output       : SongFolder/stem.m4a  (same CDN path as .mp3)\n")

    start   = time.time()
    done    = success = fail = 0

    with ThreadPoolExecutor(max_workers=CONCURRENT) as ex:
        futures = {ex.submit(process_mp3, p, prog): p for p in remaining}
        for future in as_completed(futures):
            ok, path, msg = future.result()
            done += 1
            if ok and msg != "skipped":
                success += 1
            elif not ok:
                fail += 1
                print(f"  ✗ {path.parent.name}/{path.name}: {msg}")

            if done % 50 == 0 or done == len(remaining):
                elapsed = time.time() - start
                rate    = done / elapsed * 60 if elapsed > 0 else 0
                pct     = done / len(remaining) * 100
                eta     = (len(remaining) - done) / (done / elapsed) / 60 if done > 0 else 0
                print(f"  [{done}/{len(remaining)}] {pct:.1f}% | "
                      f"✓{success} ✗{fail} | "
                      f"{rate:.1f} songs/min | ETA {eta:.0f} min")

    elapsed = time.time() - start
    print(f"\n{'=' * 60}")
    print(f"✅ Done! {success} uploaded, {fail} failed")
    print(f"⏱  {elapsed / 60:.1f} minutes total")

if __name__ == "__main__":
    main()
