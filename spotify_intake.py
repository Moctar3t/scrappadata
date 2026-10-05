#!/usr/bin/env python3
"""
spotify_intake.py: one Spotify playlist is the catalogue's inbox.

Add a song to that playlist (phone, desktop, anywhere) and the next pull claims
it: the same ingest every other song goes through (the source verified against
Spotify's runtime, stems split on Replicate (the replicate line: no home GPU),
BPM and key measured, Genius credits,
La boîte bleue's six vibe numbers, published to the app). A song already in the
catalogue is claimed without being added twice.

  zingy.py spotify login            once: authorise as the playlist's owner
  zingy.py spotify use "<link>"     which playlist is the inbox (a link or its name)
  zingy.py spotify pull [--apply]   claim what is new (without --apply: just list it)
  zingy.py spotify status           the inbox, the login, what became of each song
  zingy.py spotify schedule on|off  pull --apply every 30 min while the Mac is awake

Why a login and not the app's own key: since Feb 2026 Spotify returns a
playlist's songs only to its owner or a collaborator, so the pull reads it as
you (PKCE, no secret involved). Spotify's login lasts 180 days for this app; a
pull after that says so and `login` renews it.

Files (all private, none of them committed):
  .spotify.json         client_id (+ client_secret for the lookup-only fallback), playlist
  .spotify_token.json   your refresh token
  spotify_intake.json   what was claimed, when, and how
"""
from __future__ import annotations
import base64, csv, fcntl, hashlib, json, os, re, secrets, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CONFIG = HERE / ".spotify.json"
TOKEN = HERE / ".spotify_token.json"
STATE = HERE / "spotify_intake.json"
LOCK = HERE / ".spotify_intake.lock"
INBOX_CSV = HERE / "spotify_inbox.csv"
LOGFILE = HERE / "spotify_intake.log"
PLIST = Path.home() / "Library/LaunchAgents/systems.dime.zingy.spotify.plist"
LABEL = "systems.dime.zingy.spotify"

PORT = 8888
REDIRECT = f"http://127.0.0.1:{PORT}/callback"
SCOPES = "playlist-read-private playlist-read-collaborative"
API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"

class SpotifyError(RuntimeError): pass

def log(m): print(m, flush=True)
def now_iso() -> str: return time.strftime("%Y-%m-%dT%H:%M:%S")

def _read(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default

def _write_private(p: Path, obj) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)

def config(*need: str) -> dict:
    cfg = _read(CONFIG, {})
    gone = [k for k in need if not cfg.get(k)]
    if gone:
        hint = {"client_id": f"put the app's client_id in {CONFIG.name}",
                "playlist": 'choose the inbox: zingy.py spotify use "<playlist link>"'}
        raise SpotifyError("; ".join(hint.get(k, f"{k} missing in {CONFIG.name}") for k in gone))
    return cfg

# ── tokens ──────────────────────────────────────────────────────────────────
def _post_token(form: dict, basic: str | None = None) -> dict:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if basic:
        headers["Authorization"] = f"Basic {basic}"
    req = urllib.request.Request(f"{ACCOUNTS}/api/token", data=urllib.parse.urlencode(form).encode(),
                                 headers=headers)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=25).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if "invalid_grant" in body:
            raise SpotifyError("the Spotify login has expired or was revoked: zingy.py spotify login")
        raise SpotifyError(f"token refused ({e.code}): {body[:200]}")

def _save_token(tok: dict, previous: dict | None = None) -> dict:
    keep = {
        "access_token": tok["access_token"],
        "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)),
        # Spotify sometimes hands a new refresh token on refresh, sometimes not.
        "refresh_token": tok.get("refresh_token") or (previous or {}).get("refresh_token"),
        "scope": tok.get("scope") or (previous or {}).get("scope"),
        "logged_in_at": (previous or {}).get("logged_in_at") or now_iso(),
    }
    _write_private(TOKEN, keep)
    return keep

def user_token() -> str | None:
    """The owner's access token, refreshed when it is about to run out. None if
    nobody has logged in."""
    tok = _read(TOKEN, None)
    if not tok or not tok.get("refresh_token"):
        return None
    if tok.get("expires_at", 0) - 60 > time.time():
        return tok["access_token"]
    cfg = config("client_id")
    fresh = _post_token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                         "client_id": cfg["client_id"]})
    return _save_token(fresh, tok)["access_token"]

def app_token() -> str:
    """Client credentials: the app alone, for catalogue lookups when nobody has
    logged in (album / track adds). Needs the client_secret."""
    cfg = config("client_id", "client_secret")
    basic = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    return _post_token({"grant_type": "client_credentials"}, basic)["access_token"]

def bearer() -> str:
    return user_token() or app_token()

def api(path: str, tok: str | None = None, **params) -> dict:
    url = f"{API}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    tok = tok or bearer()
    for attempt in range(5):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=25).read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429:
                time.sleep(min(30, int(e.headers.get("Retry-After") or 3)))
                continue
            if e.code == 401 and attempt == 0 and _read(TOKEN, None):
                t = _read(TOKEN, {}); t["expires_at"] = 0; _write_private(TOKEN, t)
                tok = bearer(); continue
            if e.code >= 500:
                time.sleep(2 * (attempt + 1)); continue
            msg = body
            try:
                msg = json.loads(body).get("error", {}).get("message") or body
            except Exception:
                pass
            raise SpotifyError(f"Spotify {e.code} on /{path.split('?')[0]}: {str(msg)[:200]}")
        except urllib.error.URLError as e:
            if attempt == 4:
                raise SpotifyError(f"Spotify unreachable: {e.reason}")
            time.sleep(2 * (attempt + 1))
    raise SpotifyError(f"Spotify kept rate-limiting /{path}")

# ── login ───────────────────────────────────────────────────────────────────
def login() -> None:
    cfg = config("client_id")
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    url = f"{ACCOUNTS}/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": cfg["client_id"], "scope": SCOPES,
        "redirect_uri": REDIRECT, "state": state,
        "code_challenge_method": "S256", "code_challenge": challenge,
    })
    got: dict = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != "/callback":
                self.send_response(404); self.end_headers(); return
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()})
            ok = "code" in got and got.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("spotify relié, tu peux fermer cet onglet" if ok else "refusé").encode())
        def log_message(self, *a):
            pass

    try:
        srv = HTTPServer(("127.0.0.1", PORT), Callback)
    except OSError:
        raise SpotifyError(f"port {PORT} is taken: close what uses it and run login again")
    srv.timeout = 1
    log(f"  log in as the playlist's owner (the browser opens; if not, open this):\n  {url}\n")
    log(f"  the app's settings on developer.spotify.com must list this redirect URI: {REDIRECT}")
    webbrowser.open(url)
    deadline = time.time() + 300
    while not got and time.time() < deadline:
        srv.handle_request()
    srv.server_close()
    if not got:
        raise SpotifyError("no answer from Spotify within 5 minutes")
    if got.get("state") != state:
        raise SpotifyError("the answer did not come from this login (state mismatch)")
    if "error" in got:
        raise SpotifyError(f"Spotify said no: {got['error']}")
    tok = _post_token({"grant_type": "authorization_code", "code": got["code"], "redirect_uri": REDIRECT,
                       "client_id": cfg["client_id"], "code_verifier": verifier})
    tok = _save_token(tok, {"logged_in_at": now_iso()})
    me = api("me", tok["access_token"])
    log(f"  ✓ logged in as {me.get('display_name') or me.get('id')}")

# ── the inbox ───────────────────────────────────────────────────────────────
def playlist_id(raw: str) -> str | None:
    m = re.search(r"playlist[:/]([A-Za-z0-9]{22})", raw) or re.fullmatch(r"\s*([A-Za-z0-9]{22})\s*", raw)
    return m.group(1) if m else None

def use(raw: str) -> None:
    pid = playlist_id(raw)
    if not pid:
        # A name: one of the logged-in owner's own playlists, matched exactly.
        want = raw.strip().lower()
        offset, hits = 0, []
        while True:
            page = api("me/playlists", limit=50, offset=offset)
            hits += [p for p in page.get("items", []) if p and (p.get("name") or "").strip().lower() == want]
            if not page.get("next"):
                break
            offset += 50
        if len(hits) != 1:
            raise SpotifyError(f"{len(hits)} of your playlists are named {raw!r}: paste its link instead")
        pid = hits[0]["id"]
    meta = api(f"playlists/{pid}", fields="id,name,owner(id,display_name),collaborative")
    probe = api(f"playlists/{pid}/items", limit=1)
    if "items" not in probe:
        raise SpotifyError(f"{meta.get('name')!r} is not yours or shared with you as a collaborator: "
                           "Spotify only gives its songs to its owner")
    cfg = _read(CONFIG, {})
    cfg.update({"playlist": pid, "playlist_name": meta.get("name")})
    _write_private(CONFIG, cfg)
    log(f"  ✓ inbox: {meta.get('name')} ({probe.get('total', 0)} songs) by "
        f"{(meta.get('owner') or {}).get('display_name') or (meta.get('owner') or {}).get('id')}")

def playlist_songs(pid: str) -> tuple[list[tuple[dict, dict]], int]:
    """(entry, track) for every real song in the playlist, in its order, and how
    many entries were skipped (local files, podcast episodes, removed songs)."""
    out, skipped, offset = [], 0, 0
    while True:
        page = api(f"playlists/{pid}/items", limit=50, offset=offset, additional_types="track")
        if "items" not in page:
            raise SpotifyError("Spotify returned the playlist without its songs: log in as its owner")
        for it in page["items"]:
            tr = it.get("item") or it.get("track")   # "track" is the pre-2026 name
            if it.get("is_local") or not tr or tr.get("type") != "track" or not tr.get("id"):
                skipped += 1; continue
            out.append((it, tr))
        if not page.get("next"):
            break
        offset += 50
    return out, skipped

def load_state() -> dict:
    return _read(STATE, {"claimed": {}})

def save_state(st: dict) -> None:
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)

def _manifest_by_uri() -> dict:
    import ingest_gpu as ig
    return {t.get("spotify_uri"): t for t in ig.load_state()["tracks"] if t.get("spotify_uri")}

def pull(apply: bool, ingest) -> None:
    """Claim what is new in the inbox. `ingest(csv_path)` runs the stages."""
    import ingest_gpu as ig
    import build_add_csv as bac
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("  another pull is running, leaving it to that one."); return
    cfg = config("client_id", "playlist")
    st = load_state()
    claimed = st.setdefault("claimed", {})
    songs, skipped = playlist_songs(cfg["playlist"])
    fresh = [(it, tr) for it, tr in songs if tr["id"] not in claimed]
    log(f"  inbox {cfg.get('playlist_name') or cfg['playlist']}: {len(songs)} song(s), "
        f"{len(fresh)} new" + (f", {skipped} skipped (local files, episodes)" if skipped else ""))
    st["last_pull"] = now_iso()
    if not fresh:
        save_state(st); return

    cat = ig.local_index()
    idx: dict = {}
    for s in cat.get("songsList", []):
        idx.setdefault(ig.ntitle(s.get("title", "")), []).append((ig.atoks(s.get("artist", "")), s.get("id")))
    in_manifest = _manifest_by_uri()

    def catalogue_id(artist: str, title: str):
        cand = ig.atoks(artist)
        for toks, cid in idx.get(ig.ntitle(title), []):
            if cand and toks and cand & toks:
                return cid
        return None

    rows, known = [], []
    for it, tr in fresh:
        artist = "; ".join(a["name"] for a in tr.get("artists", []))
        rec = {"artist": artist, "title": tr.get("name", ""), "added_at": it.get("added_at"),
               "uri": tr.get("uri")}
        cid = catalogue_id(artist, rec["title"])
        if cid is not None:
            known.append((tr["id"], {**rec, "how": "catalogue", "catalog_id": str(cid)}))
        elif tr.get("uri") in in_manifest:
            known.append((tr["id"], {**rec, "how": "ingest"}))
        else:
            r = bac.row(tr)
            r["Added At"] = it.get("added_at") or ""
            rows.append((tr["id"], rec, r))

    for tid, rec in known:
        what = f"already in the catalogue (#{rec['catalog_id']})" if rec["how"] == "catalogue" else "already in the ingest"
        log(f"    = {rec['artist'][:26]:26s} {rec['title'][:30]:30s} {what}")
    for _tid, rec, _r in rows:
        log(f"    + {rec['artist'][:26]:26s} {rec['title'][:30]:30s}")
    if not apply:
        log(f"  would add {len(rows)} song(s) (dry run: pass --apply)"); return

    for tid, rec in known:
        claimed[tid] = {**rec, "claimed_at": now_iso()}
    save_state(st)
    if not rows:
        return
    with open(INBOX_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=bac.COLUMNS)
        w.writeheader()
        for _tid, _rec, r in rows:
            w.writerow(r)
    ingest(str(INBOX_CSV))
    # Claimed once the ingest holds it: from there its retries are the ingest's
    # (resolve tries an unmatched song again every run, produce a failed one).
    in_manifest = _manifest_by_uri()
    st = load_state()
    claimed = st.setdefault("claimed", {})
    for tid, rec, _r in rows:
        t = in_manifest.get(rec["uri"])
        if t is not None:
            claimed[tid] = {**rec, "how": "ingest", "claimed_at": now_iso()}
    save_state(st)

def status() -> None:
    cfg = _read(CONFIG, {})
    tok = _read(TOKEN, None)
    unset = 'none (zingy.py spotify use "<link>")'
    log(f"  inbox:  {cfg.get('playlist_name') or cfg.get('playlist') or unset}")
    if tok and tok.get("refresh_token"):
        since = tok.get("logged_in_at", "?")
        log(f"  login:  since {since} (Spotify ends it after 180 days)")
    else:
        log("  login:  none (zingy.py spotify login)")
    log(f"  schedule: {'on, every 30 min' if PLIST.exists() else 'off'}")
    st = load_state()
    claimed = st.get("claimed", {})
    log(f"  last pull: {st.get('last_pull', 'never')} · {len(claimed)} song(s) claimed")
    if not claimed:
        return
    man = _manifest_by_uri()
    counts: dict = {}
    for rec in claimed.values():
        if rec.get("how") == "catalogue":
            word = "in the catalogue"
        else:
            word = (man.get(rec.get("uri")) or {}).get("status", "?")
        counts[word] = counts.get(word, 0) + 1
    log("  " + " · ".join(f"{n} {w}" for w, n in sorted(counts.items(), key=lambda x: -x[1])))
    for rec in list(claimed.values())[-15:]:
        t = man.get(rec.get("uri")) or {}
        where = (f"#{rec['catalog_id']} (was there)" if rec.get("how") == "catalogue"
                 else f"#{t.get('catalog_id', '?')} {t.get('status', '?')}"
                      + (f": {t['error'][:50]}" if t.get("error") else ""))
        log(f"    {rec['artist'][:24]:24s} {rec['title'][:28]:28s} {where}")

# ── the 30-minute pull ──────────────────────────────────────────────────────
def schedule(on: bool) -> None:
    if not on:
        if PLIST.exists():
            subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
            PLIST.unlink()
        log("  schedule off"); return
    path = ":".join(dict.fromkeys([str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/local/bin",
                                   str(Path.home() / "Library/Python/3.9/bin"), "/usr/bin", "/bin",
                                   "/usr/sbin", "/sbin"]))
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array>
<string>{sys.executable}</string><string>{HERE / 'zingy.py'}</string>
<string>spotify</string><string>pull</string><string>--apply</string></array>
<key>WorkingDirectory</key><string>{HERE}</string>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string></dict>
<key>StartInterval</key><integer>1800</integer>
<key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>{LOGFILE}</string>
<key>StandardErrorPath</key><string>{LOGFILE}</string>
</dict></plist>
""", encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    subprocess.run(["launchctl", "load", str(PLIST)], check=True)
    log(f"  schedule on: every 30 min while the Mac is awake, log in {LOGFILE.name}")
