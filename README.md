# Zingy catalogue — the toolset

One entry point: **`zingy.py`**. Everything else is a library it drives.

```bash
python3 zingy.py add --album "Ken Carson/xperiment"     # add a whole album
python3 zingy.py add --track "The Smiths - How Soon Is Now?"
python3 zingy.py add --csv ~/Desktop/playlist.csv       # Exportify CSV
python3 zingy.py audit --deep 40                        # health check, read-only
python3 zingy.py fix romaji --apply                     # romanise Japanese titles
python3 zingy.py fix artists --apply                    # romanise Japanese artist names
python3 zingy.py fix covers --apply                     # replace wrong cover art
python3 zingy.py fix producers --apply                  # recover credits Genius has
python3 zingy.py fix bpmkey --apply                     # measure BPM/key on the VPS
python3 zingy.py fix vibe --apply                       # La boîte bleue's six numbers, on the VPS
python3 zingy.py spotify pull --apply                   # claim what is new in the Spotify inbox
python3 zingy.py dedupe --apply                         # songs the app hides as dupes
python3 zingy.py repair --apply                         # re-split songs with no stems
python3 zingy.py relabel 3644 --title "MAKE A SOUND" --artist "Destroy Lonely" --apply
python3 zingy.py verify --ids 9 77 4548                 # re-check against the LIVE CDN
```

## Files

| file | role |
|---|---|
| `zingy.py` | the CLI — add / audit / fix / relabel / verify |
| `ingest_gpu.py` | the ingest engine (Mac drives the GPU box over SSH) |
| `gpu_agent.py` | the only thing that runs on the Windows box; holds no secrets |
| `catalog_lib.py` | the ONE way to read/edit/publish/verify the catalogue |
| `romaji.py` | Japanese → romaji, with guardrails |
| `build_add_csv.py` | Spotify → Exportify-shaped CSV (gets authoritative durations) |
| `detect_clean.py` | screens for censored/radio edits from the vocal stem (VPS) |
| `vps_analyze.py` | measures BPM + key from stems, on the VPS |
| `replicate_line.py` / `vps_line.py` | the replicate line: Mac tunnel + calls / the VPS worker (search, fetch, Replicate split, encode, BPM/key) |
| `spotify_intake.py` | the Spotify playlist inbox: login (PKCE, as the owner), use, pull, status, schedule |
| `vibe.py` / `vps_vibe.py` | La boîte bleue: valence, danceability, energy, acousticness, speechiness, instrumentalness from the stems, on the VPS (the `vibe` stage and `fix vibe`) |
| `retired/` | superseded one-off scripts, kept only for reference |

## The Spotify inbox

One playlist you own is the inbox: add a song to it anywhere, and `spotify pull
--apply` (by hand, or every 30 min with `spotify schedule on`) runs it through the
whole ingest on the **replicate line**: no home GPU. The VPS searches, fetches,
splits on Replicate (ryan5453/demucs `htdemucs_ft`, FLAC out, the model the
server uses for uploads), encodes once to AAC 128k and measures BPM/key. YouTube
bot-walls the VPS, so the Mac opens an `ssh -4 -R` SOCKS tunnel out of its own
connection for each stage (IPv4: YouTube signs audio links per address, and an
IPv6-signed link fetched over IPv4 is a 403). Any add can use it:
`zingy.py add --album "A/B" --line replicate`. Checked on Mitski "Washing
Machine Heart" against the box's stems of the same upload: same length,
loudness within 0.4 LU per stem. Replicate GPU time is about 17 to 23 s a song,
2 min end to end. yt-dlp on the VPS must stay current (2026.03 could no longer
download; updated to 2026.08.19 on 2026-10-05, old copy at
/usr/local/bin/yt-dlp.2026.03.17.bak). A song already in the catalogue is claimed without a second copy.
Setup, once: the Spotify app needs Premium on its owner's account (Feb 2026 rule)
and the redirect URI `http://127.0.0.1:8888/callback`; then `spotify login`, then
`spotify use "<playlist link>"`. Since Feb 2026 a playlist's songs are returned only
to its owner or a collaborator, hence the login. It lasts 180 days.

Spotify gives no audio features to new apps, so these songs get their six vibe
numbers from La boîte bleue (`vibe` stage). Checked on 12 songs it never trained
on, against their Spotify values: correlation 0.94 energy, 0.87 danceability,
0.98 instrumentalness, 0.80 speechiness, 0.77 valence, 0.72 acousticness; it pulls
the extremes toward the middle.

## The rules the pipeline enforces

1. **Never invent a value.** A field we could not source is omitted, never
   defaulted. There is no "120 BPM fallback".
2. **Never advance a status without proof.** `uploaded` needs a 2xx from the PUT;
   `verified` needs a 200 from a real HEAD against the CDN.
3. **Never guess the recording.** A source is accepted only if its runtime is
   within `DURATION_TOLERANCE_S` of Spotify's **and** its title names the song.
4. **The CSV/Spotify is authoritative** for bpm, key, duration, album, label,
   release date, genres, features, popularity. We don't re-derive what we were told.

## Bugs these rules exist because of

Each one was live in the catalogue, not hypothetical.

- **Right artist, wrong song.** id 9 was titled *Plastic Love* but played
  *Namida no One-sided Love* — same artist, official Topic channel, 291 s against
  Plastic Love's 294 s, so a duration-only gate passed it. id 3644 *presidential*
  played Destroy Lonely's *MAKE A SOUND* the same way. **Fix:** `title_matches()`
  — the candidate's title must name the song. Two more of today's own ingests
  (*Goodbye* → *Walked Outta Heaven*, *I Wanna Know* → *I Believe In You*) were
  caught by it immediately.
- **The trailing newline.** `upload-catalog.sh` reads its file list with
  `while read`, which silently drops a final line with no newline — and that line
  was `music-index.json`. Stems and per-song JSON went live while the app still
  saw the old index. **Fix:** `catalog_lib.publish()` always terminates the list
  and then re-fetches the live index to prove the ids landed.
- **cp850 ate the titles.** The box's French console mangled a YouTube title
  written in Unicode math-italic (`𝟏𝟏𝟐 - 𝑷𝒆𝒂𝒄𝒉𝒆𝒔 & 𝑪𝒓𝒆𝒂𝒎` arrived as `-  &  ( )`),
  which then defeats title verification. **Fix:** the agent emits `ensure_ascii=True`.
- **Wrong cover from a fuzzy match.** iTunes `limit=1` with no artist check gave
  Yurie Kokubu's song DJ Khaled's *Suffering From Success* sleeve, gave
  Junko Ohashi a Various-Artists compilation sleeve, and gave a Jodeci remix the
  art of a *Star* TV-cast cover. **Fix:** the result must match artist **and**
  title, and Various Artists is refused — in the ingest and in `fix covers`.
- **Blind `hits[0]` from Genius.** Searching "Jodeci Come & Talk To Me" returns
  the *Remix* first, which would write the wrong credits. **Fix:** hits are scored.
- **Invented romaji.** Sources return page labels, not titles — `黄昏のBAY CITY`
  → "Romanized", `接吻` → "ORIGINAL LOVE, 1993", `内気なジュリエット` → "Juliet".
  **Fix:** `romaji.py` rejects qualifier-only strings, artist-name echoes, and
  results that throw away most of the title, then falls back to transliteration.
- **Two commands writing the index at once.** Every command does
  load → mutate → save on the WHOLE index, so concurrency is last-writer-wins
  over the entire catalogue. A `fix producers` run that had loaded the index
  before `dedupe` ran wrote its stale copy back and silently restored all seven
  duplicates — including a song with no stems. **Fix:** `save_index` hashes the
  file at load and refuses to write if it changed underneath. `ingest_gpu` also
  holds a PID lock on its own manifest for the same reason.
- **Long-vowel romanisation.** Spotify writes "Yugure no Tori", every upload
  writes "Yuugure no Tori", and strict token matching reads those as different
  songs. **Fix:** `title_matches` falls back to a vowel-folded comparison
  (ō/ou/oo, ū/uu/u) — strict matching runs first, so ordinary titles are
  unaffected and the wrong-song rejections still fail correctly.
- **Two albums, one name.** "xperiment" (22 tracks) and "xperiment - cartunez"
  (35 tracks) are different Ken Carson releases. Searching the shorter name and
  taking the first hit silently ingests the smaller edition. **Fix:**
  `find_album` requires an exact name match, accepts a pasted Spotify album id,
  and prints the near-misses rather than guessing. (`limit` above 10 on those
  searches returns HTTP 400 — capped.)
- **`scp` and a non-ASCII username.** `dimé@host:path` works as a destination but
  its SFTP *source* parser rejects it. **Fix:** `-o User=`.

## Infrastructure notes

- The GPU box is reached over Tailscale at `100.76.91.119` with **key auth**
  (`~/.ssh/id_ed25519_gpu`). Because the account is an Administrator, the key
  lives in `C:\ProgramData\ssh\administrators_authorized_keys` — Windows OpenSSH
  ignores the per-user file for admins. No password is needed anywhere.
- Secrets (BunnyCDN key, Genius token) stay on the Mac. `gpu_agent.py` holds none.
- The VPS **no longer has a GPU tier**: zingy-api commit `9dba51a` removed the
  worker's claim/complete/fail routes on 2026-08-28, so `ZingyWorker` is disabled
  on the box. This toolset does not use that tier. Re-enable the task only if the
  routes come back.
- Stem layout is **not uniform**: newer songs are `catalogtracks/<uuid>/<stem>.m4a`,
  older ones `"<Artist - Title>/<stem>.m4a"`. Always read the URL from the
  per-song JSON (`catalog_lib.stem_urls`) instead of assuming a layout.

## Known-open

Run `python3 zingy.py audit --deep 40` for live numbers; these were true at the
close of 2026-08-30 (catalogue 4170 songs).

- **17 songs are unplayable** — their detail JSON declares no stems, so the app
  lists them and plays silence. `zingy repair` fixed 20 of the original 37; the
  rest have no source the gate will accept (mostly SoundCloud-era underground
  uploads and snippets). Re-run `zingy repair` if better sources appear.
- **88 low-res covers + 11 with none.** These are the ones where no source has a
  replacement that matches artist AND title. Loosening that check is what caused
  the wrong covers in the first place, so they are left alone deliberately.
- **491 songs have no producer credits**, and this is mostly NOT recoverable: of
  507 checked, ~58% have no Genius page at all and ~34% have a page with no
  credits filled in. `zingy fix producers` recovered 16. Genius crowd data is
  also sometimes junk — it credited a 1984 Japanese track to a singer born in
  1998, with a correct song match.
- **352 songs have no genres.**
- **BPM/key on the 40 songs added by album/track lookup are MEASURED, not label
  data.** Validated against 8 songs with known Spotify values: **BPM 5/8, key
  4/8**. BPM errors are octave (all move toward librosa's 120 BPM prior); key
  errors are tonic/dominant confusion. Two attempted fixes were tried and
  reverted for making it worse. The real fix is a trained model — there are now
  ~4137 songs with Spotify-authoritative bpm/key *and* isolated stems, a
  labelled set the size La boîte bleue trained on.
- **Censored/radio edits:** `detect_clean.py` ranks candidates from vocal-stem
  gaps but **cannot confirm** them — separated stems always retain bleed, so the
  digital-silence signature a censor leaves is not measurable from stems.
