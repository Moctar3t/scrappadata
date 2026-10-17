# How to add songs to Zingy

## The easy way: paste a link (2026-10)

**https://zingy.dime.systems/api/admin/catalogue** (DIME ADMIN → CATALOGUE → AJOUTS)

Paste a **Spotify** track / album / playlist link or a **YouTube** video / playlist link,
press AJOUTER. Within 2 minutes (if the Mac is awake) the Mac picks it up and runs the
whole ingest on the **Replicate line** (no GPU box needed): identification, source,
stems, BPM/key, Genius credits, cover, publish. The page shows each song's status live
and says when the Mac last looked at the queue. 100 songs max per link.

- YouTube link: if Deezer knows the song and your video is the same length (±5 s), your
  video is the source; if it's another cut (clip intro), the original release is used;
  if nobody knows it (leak, freestyle), it's ingested from the video itself.
- From the terminal: `python3 zingy.py intake submit "<link>"`, `… intake status`,
  `… intake poll` (run now), `… intake schedule on|off` (the 2-minute poll, on).
- Code: `link_intake.py` (link → songs), `intake_worker.py` (the Mac side),
  zingy-api `src/intake` + `src/admin/admin-intake.controller.ts` (queue + page).

**CSV files** still go through `python3 zingy.py add --csv <file> [--line replicate]`
(or `ingest_gpu.py all --csv <file>` for the GPU box).

> ⚠️ Everything below describes `auto_ingest.py` + the VPS split queue, which stopped
> working on 2026-08-28 when the API dropped the GPU worker routes. Kept for history.

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
