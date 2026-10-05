# Zingy Catalog Pipeline — the dummy-proof guide

> **Just want to add songs?** Read **`HOW_TO_ADD_SONGS.md`** instead — one command
> (`python3 auto_ingest.py "<link>"`) does stems + metadata + CDN + app. This file is
> the deeper reference (internals, the manual GPU-box pipeline, every error code).

This is how you add songs to the Zingy app: take a YouTube link (or playlist, or a Spotify
CSV), turn each song into 4 separated **stems** (bass / drums / other / vocals) on the GPU PC,
push them to the CDN, and check they actually work. One script does all of it: **`zingy_pipeline.py`**.

> **If you read nothing else, read this.** You run **4 commands in order**, on the GPU PC:
> ```
> python zingy_pipeline.py prepare --yt-playlist "PASTE_A_YOUTUBE_PLAYLIST_LINK"
> python zingy_pipeline.py run
> python zingy_pipeline.py upload
> python zingy_pipeline.py sweep
> ```
> If something breaks, scroll to **[ERROR CODES](#error-codes--what-they-mean--how-to-fix)**. Almost
> every problem is one of: the worker is pointed at a dead API domain, yt-dlp is stale, or the upload
> key is wrong. (Cookies are almost never the answer — see 2d.)

---

## 0. THE REGULAR PIPELINE (the one that built the catalog — and is running right now)

`zingy_pipeline.py` (below) is the **manual, standalone** way to run a batch by hand. The catalog was
actually built — and keeps running — through the **VPS queue + GPU worker**:

1. **Enqueue** a song on the VPS: `POST https://zingy.dime.systems/api/internal/split/catalog`
   with header `x-worker-secret: <WORKER_SECRET from /var/www/zingy-api/.env>` and JSON
   `{"youtubeUrl": "...", "title": "...", "artist": "...", "coverUrl": "..."}`.
   (Or use `scrappadata/auto_ingest.py "<yt url | csv>"` which gates/dedups/enqueues/exports for you.)
2. **The worker** on the GPU PC (`C:\Users\Dimé\demucs_relay\worker.py`, run by the **`ZingyWorker`
   scheduled task**) claims queued rows, downloads with **bare yt-dlp (no cookies)**, batch-runs
   `htdemucs_ft` on the GPU, encodes MP3 192k + M4A 128k, uploads to
   `videobb/catalogtracks/<track-uuid>/`, computes BPM/key (librosa), and reports the row `ready`.
3. **Publish**: update `musicdata/music-index.json` + `musicdata/songs/<id>.json` and PUT them to
   `videobb/musicdata/` + purge those two CDN URLs. The app fetches `musicdata/` from the CDN
   (bundle is only a fallback) — **no app rebuild needed**.

**The #1 failure mode (this exact thing burned a whole night in 2026-06):** the worker looks alive but
nothing ever completes. Check `demucs_relay\worker.log` — if you see
`[warn] claim error: ... Failed to resolve 'split.zingy.fun'` (or any `NameResolutionError`), the
worker's API base is a **dead domain**. Fix `demucs_relay\worker.env`:
```
ZINGY_API_BASE=https://zingy.dime.systems/api
```
then `schtasks /End /TN ZingyWorker & schtasks /Run /TN ZingyWorker`. Downloads were never the problem.

---

## 1. What the 4 commands do (the mental model)

Think of it as an assembly line. Each song moves through 5 states:

```
   prepare            run                        upload          sweep
 ┌─────────┐   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────┐
 │ queued  │──▶│ downloaded       │──▶│ separated        │──▶│ uploaded     │──▶ verified ✅
 │         │   │ (mp3 on disk)    │   │ (4 stems on disk │   │ (stems on    │   │ (CDN serves  │
 │ (in the │   │                  │   │  + bpm/key/cover)│   │  the CDN)    │   │  them)       │
 │  list)  │   └──────────────────┘   └──────────────────┘   └──────────────┘   └──────────────┘
 └─────────┘            run does download + separate together           sweep double-checks the CDN
```

- **`prepare`** = build the to-do list from your input, and **skip songs already in the catalogue**.
- **`run`** = the heavy one. Downloads the audio **and** runs the GPU separation. The downloader works
  *ahead* of the GPU so the GPU is never sitting idle.
- **`upload`** = sends every song's 4 stems to BunnyCDN (this is where the old pipeline died on a bad key).
- **`sweep`** = visits each stem's URL on the CDN; anything missing gets re-uploaded. A song is only
  truly "done" (`verified`) after this.

You can stop at any point (Ctrl+C) and re-run — it remembers what's done (in `pipeline_state.json`).

---

## 2. First-time setup (do this ONCE)

You only do this the first time, or if something got wiped.

### 2a. Get onto the GPU PC ("technica")
The GPU PC is reachable **only through Tailscale** (not the home Wi-Fi IP). Two ways:
- **Easiest:** sit at the PC, or Remote-Desktop into it, and open **Command Prompt** (`cmd`).
- **From your Mac terminal** (over Tailscale):
  ```
  ssh dimé@100.76.91.119        # key auth (~/.ssh/id_ed25519_gpu)
  ```

### 2b. Go to the pipeline folder
```
cd C:\Users\Dimé\Desktop\DIME_STEM_PIPELINE
```
Everything lives here: `zingy_pipeline.py`, `.env`, `PIPELINE.md` (this file), plus `tracks/` (downloaded
mp3s) and `separated/` (the stems).

### 2c. Check the `.env` file has the secrets
Open `.env` (`notepad .env`). It must have these — **if `BUNNY_STORAGE_PASSWORD` is wrong, every upload 401s**:
```
BUNNY_STORAGE_ZONE=videobb
BUNNY_STORAGE_HOST=storage.bunnycdn.com
BUNNY_STORAGE_PASSWORD=<the storage zone password>   ← the one that caused the 401s
BUNNY_API_KEY=<account key, only for CDN purges>
GENIUS_TOKEN=<for covers + producers>
YT_COOKIES_BROWSER=chrome     ← IMPORTANT for downloads (see 2d)
```
> Where to get the real `BUNNY_STORAGE_PASSWORD`: bunny.net dashboard → **Storage** → `videobb` →
> **FTP & API Access** → **Password**. Paste it in. Nothing else.

### 2d. YouTube cookies — you almost certainly do NOT need them
**Downloads work without cookies.** Proven 2026-07-02: bare `yt_dlp -f bestaudio` from the GPU PC
downloads fine (that's how all 2.6K+ songs were fetched). If a download fails, the fix is almost always
**update yt-dlp** (`"%PY%" -m pip install -U yt-dlp`), not cookies. Only if you *personally* see
`Sign in to confirm you're not a bot` on a current yt-dlp: log Chrome on the PC into YouTube and set
`YT_COOKIES_BROWSER=chrome` in `.env` as a last resort.

### 2e. One requirement: Python 3.10 or newer
Type `python --version`. If it says 3.9 or lower, **downloads will fail** (modern yt-dlp dropped 3.9).
Use the pipeline's venv python, which is 3.10+:
```
set PY=C:\Users\Dimé\demucs_relay\venv\Scripts\python.exe
"%PY%" --version
```
Then run every command with `"%PY%"` instead of `python` (e.g. `"%PY%" zingy_pipeline.py run`).

---

## 3. How to run it — pick your input

You give `prepare` **one** of these. Run `prepare` as many times as you want with different inputs — it
keeps adding new songs to the same to-do list.

| You have… | Command |
|---|---|
| A **YouTube playlist** | `python zingy_pipeline.py prepare --yt-playlist "https://youtube.com/playlist?list=..."` |
| One or more **YouTube video links** | `python zingy_pipeline.py prepare --yt "https://youtu.be/aaa" "https://youtu.be/bbb"` |
| A **Spotify CSV** (from Exportify) | `python zingy_pipeline.py prepare --csv "C:\path\to\export.csv"` |
| A plain **"Artist - Title" list** (one per line in a .txt) | `python zingy_pipeline.py prepare --tracklist "C:\path\to\list.txt"` |

Then **always** these three, in order:
```
python zingy_pipeline.py run        # downloads + GPU separates (takes a while; that's normal)
python zingy_pipeline.py upload     # pushes stems to the CDN
python zingy_pipeline.py sweep      # confirms the CDN serves them; re-uploads any that didn't
```

**That's the whole job.** When `sweep` says `N verified`, those N songs' stems are live on the CDN.

### Last step — make them show up IN THE APP (on the Mac)
The stems being on the CDN doesn't put the song in the app yet. On your **Mac**, link them into the
catalogue and publish:
```
cd /Users/dime/Developer/ios-zingy
./scripts/upload-catalog.sh        # uploads the catalogue JSON to the CDN + clears the cache
```
*(The catalogue JSON — `music-index.json` + `songs/<id>.json` — is what the app reads. The linking step
fills in the stem URLs + bpm/key/cover for each new song.)*

---

## 4. How to know what's going on (read the to-do list)

Everything's tracked in **`pipeline_state.json`** in the pipeline folder. Open it (`notepad
pipeline_state.json`) or, to see a quick summary, run any stage — they print counts.

Each song has a `"status"`:

| status | meaning | what's next |
|---|---|---|
| `queued` | on the list, nothing done | `run` will pick it up |
| `downloaded` | mp3 is on disk | `run` will separate it |
| `separated` | 4 stems made + metadata grabbed | `upload` |
| `uploaded` | stems pushed to CDN | `sweep` |
| `verified` | ✅ CDN actually serves them — **done** | publish on the Mac |
| `failed` | something broke | read its `"error"` field → see error table below |

To re-try the failed ones: fix the cause, then just run `run` / `upload` / `sweep` again. It only
re-does what isn't finished. To force a single song to retry from scratch, set its `"status"` back to
`"queued"` in the file (or delete `pipeline_state.json` to restart the whole list).

---

## ERROR CODES — what they mean & how to fix

When you start a stage it prints a banner like `== zingy_pipeline :: upload ==  (zone=videobb, key=set)`.
**If it says `key=MISSING`, stop — your `.env` isn't being read.** Otherwise, match your error below.

### Upload / CDN errors
| What you see | What it means | Fix |
|---|---|---|
| `401 Unauthorized — BUNNY_STORAGE_PASSWORD wrong/stale` | The upload key in `.env` is wrong. **This was THE bug.** | Get the current `videobb` password from the Bunny dashboard (setup 2c), paste into `.env`, re-run `upload`. |
| `>>> AUTH FAILED. Fix BUNNY_STORAGE_PASSWORD…` | Same as above (the loud summary). | Same. |
| `no BUNNY_STORAGE_PASSWORD in .env — refusing to upload` | `.env` has no key at all (or wrong filename/location). | Make sure `.env` is in the pipeline folder and has `BUNNY_STORAGE_PASSWORD=...`. |
| `http 404` in **sweep** | A stem isn't on the CDN. | `sweep` auto-re-uploads it. If it stays 404: the local stem is missing → re-run `run` for that song. |
| `http 500/503` | BunnyCDN hiccup. | Just re-run `upload` / `sweep` — it retries. |

### Worker (regular pipeline) errors — CHECK THIS FIRST
Read `C:\Users\Dimé\demucs_relay\worker.log`:
| What you see | What it means | Fix |
|---|---|---|
| `[warn] claim error: ... NameResolutionError ... 'split.zingy.fun'` | Worker polls a **dead domain** — it looks alive, does nothing, forever. **This was THE bug of 2026-06.** | `worker.env` → `ZINGY_API_BASE=https://zingy.dime.systems/api`, restart the `ZingyWorker` task. |
| `[warn] claim error: 401` | Wrong `ZINGY_WORKER_SECRET`. | Copy `WORKER_SECRET` from `/var/www/zingy-api/.env` on the VPS. |
| `bunny put 401` | Wrong `BUNNY_STORAGE_PASSWORD`. | Same fix as the upload table above. |

### Download / YouTube errors (the other big one)
These appear in a song's `error` field as `download: ...`. **99% of download problems = stale yt-dlp. Cookies are the LAST resort, not the first.**
| What you see | What it means | Fix |
|---|---|---|
| `HTTP Error 403: Forbidden` | yt-dlp too old for current YouTube (bot wall). | `"%PY%" -m pip install -U yt-dlp` and retry. Cookies (2d) only if a current yt-dlp still fails. |
| `Requested format is not available` | yt-dlp is **too old** for current YouTube. | `run` auto-updates it; if it persists, manually: `"%PY%" -m pip install -U yt-dlp`. Check Python is 3.10+ (setup 2e). |
| `The page needs to be reloaded` | Same — yt-dlp out of date. | Update yt-dlp (above). |
| `Sign in to confirm you're not a bot` | YouTube wants a logged-in session (rare from this box). | Update yt-dlp first; if it persists, add cookies (2d). |
| `download: timed out (>240s)` | A download wedged; the pipeline killed it so the GPU keeps going. | Usually that one video is throttled/blocked — give it a direct `--yt` link, or skip it. |
| `no source url` | A CSV/tracklist song couldn't be matched to any YouTube video. | Add the song via `--yt "<direct link>"` instead. |

### Separation (GPU) errors
| What you see | What it means | Fix |
|---|---|---|
| `demucs: ...CUDA out of memory...` | GPU ran out of VRAM. | Close other GPU apps; re-run `run`. (htdemucs_ft is heavy.) |
| `demucs: ...` (with a 0-byte source) | The downloaded mp3 is corrupt/empty. | Delete `tracks\<Artist - Title>.mp3`, re-run `run` (re-downloads + re-separates). |
| `run` finds nothing / instantly done | All songs already separated, **or** the to-do list is empty. | Run `prepare` first; check `pipeline_state.json` has `queued` songs. |

### "It just hangs"
- **`run` sits forever** → a download wedged. It can't anymore (240s kill built in) — but if you're on an
  old copy, Ctrl+C, update the script, retry. The GPU is fine; it's always the download.
- **SSH command shows no output for a long time** → that's normal for `run` (downloads + GPU take minutes).
  Don't panic; check `pipeline_state.json` afterward for the real result.

---

## 5. Debug playbook (when you're stuck, do this in order)

1. **Read the banner.** Start any stage. `key=MISSING`? → `.env` problem (setup 2c). `key=set`? → continue.
2. **Open `pipeline_state.json`.** Find songs with `"status":"failed"` and read their `"error"`.
3. **Match the error** in the table above. 95% of the time it's: a dead worker API base, stale yt-dlp, or the upload key.
4. **Test one thing at a time.** To test downloads alone: `"%PY%" -m yt_dlp -f bestaudio -o test.%(ext)s "<a youtube link>"` — if *that* fails, it's purely a YouTube/cookies/yt-dlp issue, nothing to do with this pipeline.
5. **Test the upload key alone:** `"%PY%" zingy_pipeline.py upload` — if you see `401`, it's the key.
6. **Still stuck?** Delete `pipeline_state.json`, re-`prepare` a *single* known-good song with `--yt`, and walk it through `run` → `upload` → `sweep` watching each step.

---

## 6. Things that will bite you (read once)

- **`ZingyWorker` is the REGULAR pipeline** (runs `demucs_relay\worker.py`) — it should stay ENABLED
  and running (re-enabled 2026-07-02 after fixing its API base). `ZingyGpuWorker` stays disabled.
  If you run a big manual `zingy_pipeline.py` batch, you can `schtasks /End /TN ZingyWorker` during it
  so they don't fight for the GPU, then `/Run` it again after.
- **The GPU PC is Tailscale-only** (`100.76.91.119`, host `technica`). The home-Wi-Fi IP `192.168.1.64`
  won't accept SSH.
- **Windows here is in French** — `tasklist`/`schtasks` print French (`Désactivé` = disabled). The home
  folder has an accent (`C:\Users\Dimé\…`); keep it in quotes.
- **Disk space:** a big batch fills `tracks/` and `separated/`. After `sweep` verifies a song, its source
  `tracks\<...>.mp3` is safe to delete (the stems are already on the CDN).
- **Secrets live ONLY in `.env`.** Never hardcode a key in a script again — that exact mistake (a dead
  hardcoded upload key) is what caused months of 401 failures.
- **Rotate keys** if they ever leak (e.g. pasted in a chat): Bunny dashboard → Storage → `videobb` →
  reset password → update `.env`.
