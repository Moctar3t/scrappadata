# Zingy App Music Pipeline Recipe

This document provides a step-by-step recipe for the end-to-end Zingy music catalog ingestion pipeline, detailing how to add new songs, run stem separations on the remote Windows GPU worker, enrich tracks with metadata, upload files, sync catalog databases, and purge CDN caches.

---

## 🏗 System Architecture & Roles

*   **Mac Orchestrator (`/Users/dime/Developer/`)**: Manages the local iOS app codebase, runs iTunes/Genius metadata scrapers, syncs the catalog JSON files, and commands the Windows worker via SSH/Expect scripts.
*   **Windows GPU Worker (`192.168.1.64`)**: Performs the heavy computational lifting (CUDA-accelerated stem separation) and uploads the stems directly to BunnyCDN Storage to conserve Mac internet bandwidth.
    *   **Root Working Directory:** `C:\Users\Dimé\Desktop\DIME_STEM_PIPELINE\`
    *   **Virtual Env Python:** `C:\Users\Dimé\demucs_relay\venv\Scripts\python.exe`
    *   **Stems Output Folder:** `C:\Users\Dimé\Desktop\DIME_STEM_PIPELINE\stems\htdemucs_ft\`
    *   **Note:** The GPU PC power state is managed manually. Do not configure scripts to shut down the machine automatically.

---

## 🍳 The Full Catalog Recipe (Step-by-Step)

### Step 1: Boot the GPU PC (Wake-on-LAN)
If the Windows GPU PC is in sleep/standby mode, boot it from your Mac:
```bash
bash /Users/dime/Developer/scrappadata/wake_gpu.sh
```
*Note: Wake-on-LAN over Wi-Fi only works from sleep/standby state. If the PC is completely shut down, it must be turned on manually.*

---

### Step 2: Sourcing the Raw Track
1. Pull the target song from YouTube or another source as a high-quality audio file (e.g. 192kbps MP3).
2. Place the raw `.mp3` file on the Windows GPU worker under:
   `C:\Users\Dimé\Desktop\DIME_STEM_PIPELINE\tracks\`

---

### Step 3: Run CUDA-Accelerated Stem Separation (`htdemucs_ft`)
We **always** use the fine-tuned `htdemucs_ft` model for superior isolation quality.

1. Trigger the separation script on the Windows GPU worker:
   ```bash
   # Run directly on Windows or trigger via Expect/SSH from Mac
   python run_demucs.py
   ```
2. The script splits all raw tracks in the queue, utilizing the RTX 3070 GPU, and outputs 4 stems (`vocals.mp3`, `bass.mp3`, `drums.mp3`, `other.mp3`) under:
   `C:\Users\Dimé\Desktop\DIME_STEM_PIPELINE\stems\htdemucs_ft\<Artist - Title>\`

---

### Step 4: Upload Stems to BunnyCDN Storage
Push the newly separated stems to BunnyCDN Storage (`videobb` storage zone):

1. Launch the remote multi-threaded uploader:
   ```bash
   # Run on Windows or trigger via Mac Expect script:
   python upload_stems.py
   ```
2. This uploads the folders directly to the root of the storage zone (e.g. `https://storage.bunnycdn.com/videobb/<Artist - Title>/`). It caches successfully completed uploads in `uploaded_cache.json` to skip them next time.

---

### Step 5: Extract Metadata (Cover Art, BPM, Key, Producers)
1. Run the local metadata scraper on your Mac:
   ```bash
   python3 /Users/dime/Developer/scrappadata/metadata_scraper.py
   ```
   *   **iTunes API Search:** Queries for high-res cover art (upscaling to `1000x1000bb`).
   *   **`librosa` Audio Analysis:** Automatically analyzes the local MP3 to extract the duration, estimate BPM, and detect the musical key (e.g. "G# Major").
   *   **Genius API Scraper:** Queries Genius to extract verified track producers.

---

### Step 6: Sync & Link the Local Catalog Databases
1. Run the local catalog linking script:
   ```bash
   python3 /Users/dime/Developer/scrappadata/smart_link_stems.py
   ```
   This matches the completed track directories to the local catalog records and outputs/updates:
   *   `App/ZingyApp/Resources/musicdata/music-index.json` (Adds the track, BPM, Key, and artwork).
   *   `App/ZingyApp/Resources/musicdata/release-formats.json`
   *   `App/ZingyApp/Resources/musicdata/songs/<SongID>.json` (Creates the lazy-loaded detail JSON containing the CDN URLs for all 4 stems).

---

### Step 7: Publish Catalog and Purge CDN Edge Cache
Once the local JSON files are updated, publish them to BunnyCDN so the iOS app picks them up:

1. Execute the catalog publisher script from the `ios-zingy` root folder:
   ```bash
   cd /Users/dime/Developer/ios-zingy
   ./scripts/upload-catalog.sh
   ```
   *This reads credentials from `scripts/.env` (gitignored), uploads the JSONs to BunnyCDN under `/musicdata/`, and purges the catalog files from the edge.*

2. **Purge Stem Files (If overwriting existing files):**
   If you have overwritten existing stems with fine-tuned ones, the CDN edge might still serve the cached standard version. To clear it, perform a full pull zone purge:
   ```bash
   curl -X POST "https://api.bunny.net/pullzone/4891857/purgeCache" \
        -H "AccessKey: <BUNNY_API_KEY>"
   ```
   *(Substitute `<BUNNY_API_KEY>` with the Account API key from `scripts/.env`).*

---

## 🛠 Developer Best Practices

1.  **Do Not Pollute Project Folders with Temp Files:**
    Keep `scrappadata` and `ios-zingy` clean of temporary test audio files, zip backups, or run logs. Always use standard macOS `/tmp/` directories or the designated LLM scratch space for temporary checks.
2.  **Verify Edge Invalidation:**
    If verifying if a file has successfully updated, bypass local cache by appending a random version query parameter:
    ```bash
    curl -s "https://dimesystemsstorage.b-cdn.net/<Path>/vocals.mp3?v=12345" | shasum -a 256
    ```
    Once the cache has been purged, the non-query-parameter URL should return the same hash as the query-parameter version.
