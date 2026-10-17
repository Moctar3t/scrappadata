# How to add songs to Zingy

## The easy way: paste a link (24/7, 2026-10)

**https://zingy.dime.systems/api/admin/catalogue** (DIME ADMIN → CATALOGUE → AJOUTS)

Paste a **Spotify** track / album / playlist or a **YouTube** video / playlist, press
AJOUTER. Within a minute the **VPS** picks it up and runs the whole ingest on the
Replicate line: identification, source, stems, BPM/key, Genius credits, cover,
publish. The Mac does not need to be on. 100 songs max per link.

The line above the queue is the system's health:
`WORKER (VPS) : il y a 30 s | SORTIE YOUTUBE : BOX ✓ · MAC ✗ | GPU BOX ✓ | LIGNE REPLICATE`

- YouTube link: Deezer knows the song and your video is the same length (±5 s) → your
  video is the source; another cut (clip intro) → the original release is found; nobody
  knows it (leak, freestyle) → ingested from the video itself.
- A song already in the app is reported (DÉJÀ DANS L'APP), never redone. Failed or partial
  links get a RELANCER button.

## Where everything runs

| piece | where | how it stays up |
|---|---|---|
| the queue + page | zingy-api (VPS) | pm2 `zingy-api` |
| the ingest worker | VPS `/opt/zingy-ingest` (this repo) | pm2 `zingy-intake`, restarts on crash and reboot |
| the manifest | VPS `/opt/zingy-ingest/ingest_gpu_state.json` | the Mac's copy is archived (`ingest_gpu_state.mac-archive.json`) |
| YouTube exit #1 | GPU box → VPS `127.0.0.1:1081` | box scheduled task `ZingyExit` (SYSTEM, at boot, reconnects) |
| YouTube exit #2 | Mac → VPS `127.0.0.1:1082` | launchd `systems.dime.zingy.exit` (while the Mac is awake) |
| YouTube exit #3 | paid residential proxy | `ZINGY_YT_PROXY=…` in `/etc/zingy/host.env` on the VPS (none yet) |
| GPU box for the VPS | box SSH → VPS `127.0.0.1:2222` | same `ZingyExit` tunnel |
| age-gate cookies | VPS `/etc/zingy/youtube_cookies.txt` | used locked, through an exit only |
| yt-dlp | VPS `/usr/local/bin/yt-dlp` | `/etc/cron.daily/yt-dlp-update` |

**Why exits:** YouTube blocks the VPS's address ("Sign in to confirm you're not a bot"),
so YouTube traffic leaves through your home connection (box or Mac). With no exit up,
links **wait** ("en attente d'une sortie YouTube") and start by themselves when one returns.

**Fallback:** a song Replicate fails on gets another go on the GPU box when the box is up.

**Two machines, one catalogue:** the VPS publishes ingests; the Mac's tools (`zingy.py fix /
relabel / dedupe …`) edit the catalogue. Every save is a three-way merge with the **live**
index, so neither can erase the other's songs.

## From the Mac (a remote control now)

```bash
python3 zingy.py add --csv ~/Downloads/playlist.csv --line gpu   # runs ON THE VPS, Mac can sleep
python3 zingy.py remote log          # follow that run
python3 zingy.py status              # the manifest (on the VPS)
python3 zingy.py intake submit "<link>"   # same as the page
python3 zingy.py remote produce --line gpu  # any ingest_gpu.py stage, on the VPS
```

`ingest_gpu.py` refuses to run on the Mac now (its manifest moved): use the above.

## When something is down

| the page says | do |
|---|---|
| `SORTIE YOUTUBE : BOX ✗ · MAC ✗` and links wait | power the box on (or open the Mac). Nothing to restart. |
| `LE WORKER NE RÉPOND PLUS` | `ssh zingy-vps 'pm2 restart zingy-intake && pm2 logs zingy-intake --lines 30'` |
| a song `SOURCE INTROUVABLE` | no YouTube upload matches its length (±5 s): paste the right YouTube video instead |
| a song `ÉCHEC … age-gated` | that video needs a signed-in, **age-verified** account: see cookies below |
| everything fails on download | `ssh zingy-vps /etc/cron.daily/yt-dlp-update` (YouTube changed) |

**Cookies (age-gated videos):** sign in to a *throwaway* YouTube account in a **private
window**, confirm it's 18+ (open an age-restricted video, complete YouTube's check), export
cookies.txt, **close the window**, then
`scp cookies.txt zingy-vps:/etc/zingy/youtube_cookies.txt && ssh zingy-vps chmod 600 /etc/zingy/youtube_cookies.txt`.
Never use the same session in two places: Google rotates it and both copies die.

**CSV files** go through `python3 zingy.py add --csv <file> [--line gpu|replicate]` (on the VPS).

> ⚠️ Everything below describes `auto_ingest.py` + the old VPS split queue, which stopped
> working on 2026-08-28 when the API dropped the GPU worker routes. Kept for history only.

---


**One command does everything.** You give it a link. It finds the real recordings, splits
them on the GPU PC, pulls producers/writers/year/label/BPM/key/cover from Genius (+ Deezer,
MusicBrainz, iTunes), uploads stems + metadata to the CDN, and the app hot-reloads it.
You never touch the GPU PC.

---

## 0. What you need (once)

- Your **Mac**, with the `scrappadata` folder (you have it: `/Users/dime/Developer/scrappadata`).
- **Tailscale ON** (the Mac reaches the GPU PC through it — the script checks/starts the
  worker for you).
- The **GPU PC powered on**. That's it — you don't log into it.

No secrets to export, no cookies, nothing: the script pulls the API secret from the VPS
itself and the Genius token is already in the folder (`.genius_token`).

---

## 1. The command

Open Terminal:

```bash
cd /Users/dime/Developer/scrappadata
python3 auto_ingest.py "PASTE_YOUR_LINK_HERE"
```

What can you paste?

| You have… | Example |
|---|---|
| A **YouTube playlist** | `python3 auto_ingest.py "https://www.youtube.com/playlist?list=PLxxxx"` |
| A **single YouTube video** | `python3 auto_ingest.py "https://www.youtube.com/watch?v=kyfJpNdjJ4A"` |
| A **Spotify CSV** (Exportify) | `python3 auto_ingest.py ~/Downloads/my_playlist.csv` |

Useful extras:
- `--limit 10` → only take the first 10 songs (good for a test run).
- `--skip-publish` → do everything except pushing to the CDN (dry-run the ingest).

---

## 2. What you'll see (stage by stage)

```
1. GATE      "accepted 24 track(s)"     → it found the REAL recording for each song
                                          (label masters > official audio > music video;
                                          covers/remixes/live versions are REJECTED)
2. DEDUP     "3 already in catalogue"   → re-running the same playlist is always safe
3. ENQUEUE   "enqueued 21 track(s)"     → they're in the VPS queue now
4. WORKER    "GPU worker: running ✓"    → it checks/starts the GPU worker FOR you
5. WAIT      "ready=8 processing=4 …"   → GPU splits in batches of 8; downloads run ahead
                                          so the GPU never sits idle. ~20-40s per song.
6. ENRICH    "got: producers, writers,  → Genius (official API) + Deezer + MusicBrainz
              year, label ✓ complete"     + iTunes fill in every field they know
7. EXPORT    "Added 21 brand new"       → app JSON files written (numeric ids, no rebuild)
8. PUBLISH   upload + cache purge       → CDN now serves the new catalogue
9. FINAL SWEEP                          → the receipt (below)
```

The **final sweep** is your proof it worked — every new song gets a line:

```
[OK] Travis Scott — CAN'T SAY  (ready, stems 4/4)  ✓prod ✓writ ✓year ✓label ✓bpm ✓key ✓cover
```

`[OK]` + `stems 4/4` + all `✓` = that song is 100% done: playable stems on the CDN and
full metadata. A `✗` on an obscure song means none of the four sources know that field —
everything else about the song still works.

**Then:** open the app (foreground it). It hot-reloads the catalogue from the CDN within
seconds. No rebuild, no reinstall.

---

## 3. How songs get their info (so you know what to expect)

| Field | Where it comes from (in order) |
|---|---|
| Producers, writers | Genius (official API, our token) |
| Year | Genius → **MusicBrainz** (knows the ORIGINAL release year — a 1980 song says 1980, not the 2017 reissue) → Deezer → iTunes |
| Label | Genius → Deezer → MusicBrainz → iTunes |
| BPM + key | The Spotify CSV if you used one (authoritative); otherwise computed from the audio on the GPU |
| Cover | **Genius art = primary**, your previous/Spotify art = automatic fallback if the Genius image ever 404s |
| Album / release format | Deezer → MusicBrainz → iTunes |

Everything found is cached (`meta_cache.json`) — re-runs are instant and free.

---

## 4. When something goes wrong

| Symptom | What it means | What to do |
|---|---|---|
| `queued` never moves, script prints "GPU worker looks dead" | Worker isn't claiming. The script restarts it automatically. | If it STAYS stuck: on the box, `type %USERPROFILE%\demucs_relay\worker.log`. `claim error … NameResolutionError` → `worker.env` must say `ZINGY_API_BASE=https://zingy.dime.systems/api`, then restart the `ZingyWorker` task. |
| "couldn't reach the GPU box" | Tailscale is off, or the PC is asleep. | Turn on Tailscale / wake the PC, re-run the same command (it resumes, nothing is lost). |
| A song was skipped: "already in catalogue" | Dedup did its job. | Nothing — that's the point. |
| The gate rejected a song ("manual", "rejected") | It couldn't find a trustworthy source (only covers/lives found). | Feed the exact video yourself: `python3 auto_ingest.py "https://youtube.com/watch?v=<the right one>"` |
| `✗year` / `✗label` on the final sweep | Genuinely obscure release. | `python3 local_metadata_enricher.py --force` retries capped songs; if the sources don't know it, fill it by hand in the song's JSON. |
| A song failed with `error=download: …` | That one video wouldn't download. | Re-run with a different video URL for that song. Do NOT add cookies — that was never the problem. |
| Publish step 429s (purge) | BunnyCDN purge rate limit under a big batch. | Re-run `bash /Users/dime/Developer/ios-zingy/scripts/upload-catalog.sh` a minute later. |
| Wrong song got fixed / need to REPLACE an existing song | This flow only ADDS. | Replacing = the swap recipe in `PIPELINE.md` §0 (enqueue exact URL → map its UUID to the song's numeric id in `catalog_id_map.json` → re-run export/publish). |

---

## 5. Sanity checks (if you like receipts)

```bash
# Is the queue moving? (statuses of everything in flight)
ssh root@204.168.236.140 'sudo -u postgres psql -d zingy -c \
  "SELECT status, count(*) FROM tracks GROUP BY status;"'

# Is a specific song live on the CDN? (replace the id)
curl -s https://dimesystemsstorage.b-cdn.net/musicdata/songs/1370.json | python3 -m json.tool

# Metadata coverage of the whole catalogue
python3 local_metadata_enricher.py --dry-run --limit 0
```

---

*Deeper internals, the GPU-box manual pipeline, and every error code: see `PIPELINE.md`.*
