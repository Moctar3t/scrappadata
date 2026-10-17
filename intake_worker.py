#!/usr/bin/env python3
"""
intake_worker.py — the Mac's side of DIME ADMIN > CATALOGUE > AJOUTS.

You paste a Spotify or YouTube link on the admin page; the VPS queues it
(catalog_intakes). Every 2 minutes, while the Mac is awake, this claims what is
queued and runs it through the SAME ingest every CSV goes through, on the
Replicate line (no home GPU):

  identify   link_intake.expand: the link → songs (Spotify embed, Deezer, YouTube)
  prepare    into the manifest; a song already in the app is reported, not redone
  resolve    the source (or the video you sent, pinned)
  produce    stems split on Replicate, BPM/key measured, uploaded and verified
  enrich     Genius credits, cover (Genius → the link's own art → iTunes)
  vibe       La boîte bleue's numbers
  publish    into the app's catalogue on the CDN

After each stage it writes back what became of each song, so the page follows.
Several links waiting at once go through the stages together.

  zingy.py intake poll              claim and process now (what the schedule runs)
  zingy.py intake submit "<link>"   queue a link from here instead of the page
  zingy.py intake status            the queue as the VPS sees it
  zingy.py intake schedule on|off   poll every 2 min while the Mac is awake

The worker secret is read from the API's .env over ssh, never stored here.
"""
from __future__ import annotations
import csv, fcntl, json, os, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

API = os.environ.get("ZINGY_API", "https://zingy.dime.systems/api")
VPS = os.environ.get("ZINGY_VPS", "204.168.236.140")
LOCK = HERE / ".intake.lock"
WORK = HERE / "intake_work"            # one CSV + prepare report per run (gitignored)
STATE = HERE / "intake_state.json"     # intake id → its songs' catalogue ids (gitignored)
LOGFILE = HERE / "intake.log"
LABEL = "systems.dime.zingy.intake"
PLIST = Path.home() / f"Library/LaunchAgents/{LABEL}.plist"
LINE = os.environ.get("ZINGY_INTAKE_LINE", "replicate")
ON_VPS = os.environ.get("ZINGY_HOST") == "vps"
API_ENV = Path("/var/www/zingy-api/.env")
WAITING = "en attente d’une sortie YouTube (box et Mac éteints) : repart tout seul dès que l’un des deux revient"

STAGES = [
    (["resolve", "--line", LINE], "recherche de la source"),
    (["produce", "--line", LINE], "séparation sur Replicate (stems, BPM, tonalité), envoi"),
    (["upload"], "envoi des stems restants"),
    (["enrich"], "crédits Genius et pochette"),
    (["vibe"], "La boîte bleue"),
    (["publish"], "publication dans l’app"),
]
GOOD = {"published", "catalogue"}

def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

# ── the VPS ─────────────────────────────────────────────────────────────────
_secret: str | None = None

def secret() -> str:
    global _secret
    if _secret is None and ON_VPS:
        for line in API_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("WORKER_SECRET="):
                _secret = line.split("=", 1)[1].strip().strip('"')
    if _secret is None:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", f"root@{VPS}",
                            "grep '^WORKER_SECRET=' /var/www/zingy-api/.env | cut -d= -f2-"],
                           capture_output=True, text=True, timeout=60)
        _secret = r.stdout.strip().strip('"')
        if not _secret:
            raise RuntimeError("couldn't read WORKER_SECRET from the VPS (ssh down?)")
    return _secret

def api(path: str, body: dict | None = None) -> object:
    req = urllib.request.Request(f"{API}/internal/intake{path}", method="POST",
                                 data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json", "x-worker-secret": secret()})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"VPS {e.code} on {path}: {e.read().decode(errors='replace')[:200]}")

def progress(iid: str, **patch) -> None:
    try:
        api(f"/{iid}/progress", patch)
    except Exception as e:                       # the page lags; the ingest must not stop for it
        log(f"  ! progress for {iid[:8]} not sent: {e}")

# ── the manifest ────────────────────────────────────────────────────────────
def manifest_busy() -> str | None:
    """Another ingest stage holding the manifest (a CSV run, the Spotify inbox)?"""
    import ingest_gpu as ig
    try:
        held = json.loads(ig.LOCK_PATH.read_text(encoding="utf-8"))
        os.kill(int(held["pid"]), 0)
        return f"{held.get('stage')} (pid {held['pid']})"
    except Exception:
        return None

def wait_manifest(max_s: int = 3600) -> bool:
    t0 = time.time()
    while (busy := manifest_busy()) and time.time() - t0 < max_s:
        log(f"  manifest busy with {busy}, waiting")
        time.sleep(30)
    return not manifest_busy()

def stage(args: list[str]) -> int:
    """One ingest_gpu.py stage. Retried once if another run grabbed the manifest in between."""
    for attempt in range(3):
        if not wait_manifest():
            return 1
        rc = subprocess.run([sys.executable, "-u", str(HERE / "ingest_gpu.py")] + args, cwd=HERE).returncode
        if rc == 0 or not manifest_busy():
            return rc
    return rc

def load_tracks() -> dict:
    import ingest_gpu as ig
    return {str(t.get("catalog_id")): t for t in json.loads(ig.STATE_PATH.read_text(encoding="utf-8"))["tracks"]}

def items_for(songs: list[dict]) -> list[dict]:
    """Each song of a link as the page shows it, from the manifest's current truth."""
    tracks = load_tracks()
    out = []
    for s in songs:
        it = {"title": s["title"], "artist": s["artist"], "catalogId": s.get("catalog_id"), "note": s.get("note")}
        if s["outcome"] == "catalogue" or (s["outcome"] == "existing" and s.get("status0") == "published"):
            it["status"] = "catalogue"
        else:
            t = tracks.get(str(s.get("catalog_id")), {})
            it["status"] = t.get("status") or "pending"
            if t.get("error") and it["status"] not in GOOD:
                it["error"] = t["error"][:300]
        out.append(it)
    return out

def verdict(items: list[dict]) -> str:
    good = sum(1 for i in items if i["status"] in GOOD)
    return "done" if good == len(items) else ("partial" if good else "failed")

# ── one poll ────────────────────────────────────────────────────────────────
def poll() -> None:
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return                                   # the previous poll is still working
    intakes = api("/claim", {"health": health()})
    if not intakes:
        return
    log(f"── {len(intakes)} lien(s) à traiter ──")
    # On the Mac: keep it awake while there is work (launchd only runs us while it is).
    awake = None if ON_VPS else subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    try:
        run(intakes)
    finally:
        if awake:
            awake.terminate()

def loop(every: int = 60) -> None:
    """The VPS service (pm2): poll forever. One bad poll never stops the next."""
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))    # pm2 stop/restart: a clean exit, not a traceback
    log(f"intake worker up on {'the VPS' if ON_VPS else 'this machine'}, line={LINE}, every {every}s")
    try:
        while True:
            try:
                poll()
            except Exception as e:
                log(f"  ! poll failed: {type(e).__name__}: {str(e)[:300]}")
            time.sleep(every)
    except KeyboardInterrupt:
        log("intake worker stopping (the manifest resumes where it was)")

def _port_open(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False

def health() -> dict:
    """What the admin page shows next to the queue. TCP checks only (is each
    tunnel up?): no YouTube request every minute while nothing is queued."""
    if not ON_VPS:
        return {"worker": "mac", "line": LINE}
    return {"worker": "vps", "line": LINE,
            "exits": {"box": _port_open(1081), "mac": _port_open(1082),
                      "proxy": bool(os.environ.get("ZINGY_YT_PROXY"))},
            "box_gpu": _port_open(int(os.environ.get("ZINGY_BOX_PORT", "2222"))),
            "cookies": Path(os.environ.get("ZINGY_YT_COOKIES", "/etc/zingy/youtube_cookies.txt")).exists()}

def rescue(live: list[dict], st: dict) -> None:
    """A song Replicate could not make gets a go on the GPU box, when it is up:
    the box splits locally and downloads from its own (home) address."""
    if LINE != "replicate":
        return
    tracks = load_tracks()
    ids = [str(s["catalog_id"]) for it in live for s in st[it["id"]]
           if (tracks.get(str(s.get("catalog_id"))) or {}).get("status") == "failed"
           and (tracks[str(s["catalog_id"])].get("line") or "gpu") == "replicate"]
    if not ids:
        return
    import ingest_gpu as ig
    # On the VPS the box is only there while its tunnel is: check that first (instant).
    if (ON_VPS and not _port_open(int(ig.BOX_PORT))) or not ig.box_alive():
        log(f"  {len(ids)} failed on Replicate; the GPU box is off, so they stay failed for now")
        return
    log(f"  {len(ids)} failed on Replicate → another go on the GPU box")
    stage(["produce", "--line", "gpu", "--relane", "--ids", ",".join(ids)])

def youtube_ready() -> bool:
    """On the VPS, YouTube is only reachable through an exit (box / Mac / proxy)."""
    if not ON_VPS:
        return True
    import replicate_line
    try:
        replicate_line.live_exit()
        return True
    except replicate_line.NoExit:
        return False

def run(intakes: list[dict]) -> None:
    import link_intake as li
    st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    if busy := manifest_busy():
        for it in intakes:
            progress(it["id"], status="claimed", stage=f"en attente : le Mac finit une autre ingestion ({busy.split(' ')[0]})")
    WORK.mkdir(exist_ok=True)

    # identify: links not yet in the manifest become rows
    fresh, rows, owner = [], [], []
    for it in intakes:
        if it["id"] in st:
            continue                             # prepared on an earlier poll: resume from the manifest
        progress(it["id"], status="processing", stage="identification du lien")
        log(f"  identify {it['url']}")
        import replicate_line
        try:
            rs, notes = li.expand(it["url"])
        except replicate_line.NoExit as e:            # YouTube unreachable: wait, don't fail
            log(f"    … {e}")
            progress(it["id"], status="claimed", stage=WAITING)
            continue
        except li.IntakeError as e:
            log(f"    ✗ {e}")
            progress(it["id"], status="failed", stage=None, error=str(e))
            continue
        except Exception as e:
            log(f"    ✗ {type(e).__name__}: {e}")
            progress(it["id"], status="failed", stage=None, error=f"identification impossible : {str(e)[:200]}")
            continue
        log(f"    {len(rs)} morceau(x)")
        progress(it["id"], status="processing", stage="préparation", notes=notes,
                 items=[{"title": r["Track Name"], "artist": r["Artist Name(s)"].replace(";", ", "),
                         "status": "identified", "note": r["Intake Note"]} for r in rs])
        fresh.append(it)
        rows += rs
        owner += [it["id"]] * len(rs)

    # prepare: one CSV for every fresh link; the report says what each row became
    if rows:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        csv_path, rep_path = WORK / f"intake-{stamp}.csv", WORK / f"intake-{stamp}.report.json"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=li.COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        rc = stage(["prepare", "--csv", str(csv_path), "--line", LINE, "--rearm", "--report", str(rep_path)])
        if rc != 0 or not rep_path.exists():
            for it in fresh:
                progress(it["id"], status="failed", stage=None, error="la préparation a échoué (voir intake.log sur le Mac)")
            return
        report = json.loads(rep_path.read_text(encoding="utf-8"))
        for r, iid, rep in zip(rows, owner, report):
            st.setdefault(iid, []).append({
                "title": r["Track Name"], "artist": r["Artist Name(s)"].replace(";", ", "),
                "note": r["Intake Note"], "outcome": rep["outcome"],
                "catalog_id": rep.get("catalog_id"), "status0": rep.get("status")})
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

    live = [it for it in intakes if it["id"] in st]
    if not live:
        return

    def report_all(stage_label: str | None, final: bool = False) -> None:
        for it in live:
            items = items_for(st[it["id"]])
            if final:
                progress(it["id"], status=verdict(items), stage=None, items=items)
            else:
                progress(it["id"], status="processing", stage=stage_label, items=items)

    work = any(s["outcome"] != "catalogue" and not (s["outcome"] == "existing" and s.get("status0") == "published")
               for it in live for s in st[it["id"]])
    if work and not youtube_ready():
        log(f"  {WAITING}")
        for it in live:
            progress(it["id"], status="processing", stage=WAITING, items=items_for(st[it["id"]]))
        return
    if work:
        for args, label in STAGES:
            report_all(label)
            log(f"══ {args[0]} ══")
            rc = stage(args)
            if rc != 0 and args[0] in ("resolve",):
                log(f"  {args[0]} failed (rc={rc}) — stopping before the long work")
                break
            if args[0] == "produce":
                rescue(live, st)
        verify(live, st)
    report_all(None, final=True)
    for it in live:
        items = items_for(st[it["id"]])
        log(f"  {it['url']} → {verdict(items)}: " + ", ".join(f"{i['title']} {i['status']}" for i in items))
        if verdict(items) == "done":
            st.pop(it["id"], None)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

def verify(live: list[dict], st: dict) -> None:
    """The receipt for THIS batch only (the full `report` sweep re-checks the whole
    catalogue and takes twenty minutes): song file + four stems, over HTTP."""
    import ingest_gpu as ig
    tracks = load_tracks()
    for it in live:
        for s in st[it["id"]]:
            t = tracks.get(str(s.get("catalog_id")))
            if not t or t.get("status") != "published":
                continue
            ok = ig.head_ok(f"{ig.CDN_HOST}/musicdata/songs/{int(t['catalog_id']):04d}.json") and \
                 all(ig.head_ok(ig.stem_url(t, x)) for x in ig.STEMS)
            if not ok:
                s["note"] = (s.get("note") or "") + " · ⚠ publié mais pas encore servi par le CDN"
            log(f"    {'✓' if ok else '✗'} #{t['catalog_id']} {t['artist']} — {t['title']}")

# ── CLI ─────────────────────────────────────────────────────────────────────
def submit(url: str) -> None:
    r = api("", {"url": url})
    print(f"  {'déjà dans la file' if r['duplicate'] else 'ajouté'} : {r['kind']} {r['url']} ({r['id'][:8]})")

def status() -> None:
    print(f"  schedule: {'on, every 2 min' if PLIST.exists() else 'off'}")
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", f"root@{VPS}",
                        "sudo -u postgres psql -d zingy -tA -F ' | ' -c \"SELECT to_char(created_at,'MM-DD HH24:MI'), "
                        "status, coalesce(stage,''), url, jsonb_array_length(items) FROM catalog_intakes "
                        "ORDER BY created_at DESC LIMIT 15\""], capture_output=True, text=True, timeout=60)
    print(r.stdout.rstrip() or "  (empty)")

def schedule(on: bool) -> None:
    if not on:
        if PLIST.exists():
            subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
            PLIST.unlink()
        print("  schedule off"); return
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
<string>intake</string><string>poll</string></array>
<key>WorkingDirectory</key><string>{HERE}</string>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string></dict>
<key>StartInterval</key><integer>120</integer>
<key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>{LOGFILE}</string>
<key>StandardErrorPath</key><string>{LOGFILE}</string>
</dict></plist>
""", encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    subprocess.run(["launchctl", "load", str(PLIST)], check=True)
    print(f"  schedule on: every 2 min while the Mac is awake, log in {LOGFILE.name}")
