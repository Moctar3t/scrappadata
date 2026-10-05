#!/usr/bin/env python3
"""
publish_status_dashboard.py — Generates a clean, terminal ASCII status
page and pushes it to BunnyCDN (purging edge cache on every update) so the
user can monitor progress from anywhere.
"""
import json, time, os, urllib.request, urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path("/Users/dime/Developer/ios-zingy")
STATE_PATH = HERE / "ingest_gpu_state.json"

def load_env() -> dict:
    env = {}
    p = REPO / "scripts/.env"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

ENV = load_env()
STORAGE_ZONE = ENV.get("BUNNY_STORAGE_ZONE", "videobb")
STORAGE_KEY = ENV.get("BUNNY_STORAGE_PASSWORD", "")
BUNNY_API_KEY = ENV.get("BUNNY_API_KEY", "")
CDN_URL = f"https://storage.bunnycdn.com/{STORAGE_ZONE}/ingest_status.html"

def generate_ascii_html(state: dict) -> str:
    tracks = state.get("tracks", [])
    total = len(tracks)
    counts = {}
    last_active = "INITIALISATION..."

    for t in tracks:
        st = t.get("status", "pending")
        counts[st] = counts.get(st, 0) + 1
        if st in ("produced", "uploaded", "verified", "published", "resolved"):
            last_active = f"{t.get('artist')} - {t.get('title')}"

    pending = counts.get("pending", 0)
    resolved = counts.get("resolved", 0)
    produced = counts.get("produced", 0)
    uploaded = counts.get("uploaded", 0)
    verified = counts.get("verified", 0)
    published = counts.get("published", 0)
    unresolved = counts.get("unresolved", 0)
    
    done = produced + uploaded + verified + published
    pct = round((done / total) * 100, 1) if total > 0 else 0.0

    # Build ASCII progress bar (width: 25 chars)
    bar_width = 25
    filled_len = int(round(bar_width * done / float(total))) if total > 0 else 0
    bar = "=" * filled_len + ">" + "." * (bar_width - filled_len - 1) if filled_len < bar_width else "=" * bar_width

    # Build ASCII activity log
    recent_lines = []
    for t in reversed(tracks):
        st = t.get("status", "pending")
        if st in ("pending",):
            continue
        tag = "[OK]  " if st in ("published", "uploaded", "verified", "produced") else "[FILE]" if st == "resolved" else "[ÉCHEC]"
        artist_title = f"{t.get('artist')} - {t.get('title')}"
        if len(artist_title) > 42:
            artist_title = artist_title[:39] + "..."
        recent_lines.append(f"  {tag} {artist_title:<42} [{st.upper()}]")
        if len(recent_lines) >= 12:
            break

    recent_block = "\n".join(recent_lines) if recent_lines else "  Aucune activité récente enregistrée."
    now_str = time.strftime("%Y-%m-%d %H:%M:%S UTC")

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="10">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate, max-age=0">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>STATUT_INGESTION // ASCII</title>
    <style>
        body {{
            background-color: #050505;
            color: #00ff66;
            font-family: 'Courier New', Courier, 'Fira Code', 'JetBrains Mono', monospace;
            font-size: 13px;
            line-height: 1.35;
            padding: 12px;
            margin: 0;
        }}
        pre {{
            margin: 0;
            white-space: pre-wrap;
            word-break: break-all;
        }}
        .c-dim {{ color: #555555; }}
        .c-bright {{ color: #ffffff; font-weight: bold; }}
        .c-cyan {{ color: #00e5ff; }}
        .c-yellow {{ color: #ffcc00; }}
        .c-green {{ color: #00ff66; }}
        .c-magenta {{ color: #ff007f; }}
    </style>
</head>
<body>
<pre>
<span class="c-dim">+-------------------------------------------------------------+</span>
| <span class="c-bright">SUPERVISION INGESTION CATALOGUE ZINGY</span>                       |
<span class="c-dim">+-------------------------------------------------------------+</span>
| STATUT     : <span class="c-green">EN COURS</span> [PIPELINE GPU ACTIF]                 |
| PROGRESSION: [<span class="c-cyan">{bar}</span>] <span class="c-bright">{pct:>5.1f}%</span>                   |
| HORODATE   : <span class="c-dim">{now_str}</span>                                |
<span class="c-dim">+-------------------------------------------------------------+</span>
| <span class="c-bright">STATISTIQUES</span>                                                  |
|   * MORCEAUX TRAITÉS  : <span class="c-cyan">{done:<6}</span> (<span class="c-dim">séparés/envoyés/publiés</span>)   |
|   * EN ATTENTE (GPU)  : <span class="c-yellow">{resolved:<6}</span> (<span class="c-dim">prêts pour séparation</span>)     |
|   * RECHERCHE EN COURS: <span class="c-dim">{pending:<6}</span>                              |
|   * NON RÉSOLUS       : <span class="c-magenta">{unresolved:<6}</span>                              |
|   * TOTAL CATALOGUE   : <span class="c-bright">{total:<6}</span>                              |
<span class="c-dim">+-------------------------------------------------------------+</span>
| <span class="c-bright">DERNIER MORCEAU TRAITÉ</span>                                      |
|   >> <span class="c-cyan">{last_active[:53]}</span>
<span class="c-dim">+-------------------------------------------------------------+</span>
| <span class="c-bright">JOURNAL D'ACTIVITÉ RÉCENTE</span>                                  |
{recent_block}
<span class="c-dim">+-------------------------------------------------------------+</span>
</pre>
</body>
</html>
"""

def upload_dashboard(html_content: str):
    headers = {
        "AccessKey": STORAGE_KEY,
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-cache, no-store, must-revalidate"
    }
    
    # Upload to both ingest_status.html and status.html
    for fn in ("ingest_status.html", "status.html"):
        target_url = f"https://storage.bunnycdn.com/{STORAGE_ZONE}/{fn}"
        req = urllib.request.Request(target_url, data=html_content.encode("utf-8"), method="PUT", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except Exception as e:
            print(f"[dashboard] Upload error for {fn}: {e}")

    # Purge BunnyCDN cache for all URL forms
    if BUNNY_API_KEY:
        for fn in ("ingest_status.html", "status.html", "ingest_status", "status"):
            purge_url = f"https://api.bunny.net/purge?url=https://dimesystemsstorage.b-cdn.net/{fn}"
            preq = urllib.request.Request(purge_url, method="POST", headers={"AccessKey": BUNNY_API_KEY})
            try:
                with urllib.request.urlopen(preq, timeout=5) as r:
                    pass
            except Exception:
                pass

def main():
    print("[dashboard] Starting ASCII live status dashboard publisher...")
    while True:
        try:
            if STATE_PATH.exists():
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    state = json.load(f)
                html = generate_ascii_html(state)
                upload_dashboard(html)
        except Exception as e:
            print(f"[dashboard] Error: {e}")
        time.sleep(10)

if __name__ == "__main__":
    main()
