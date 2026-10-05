#!/usr/bin/env python3
"""Cold-wait for the Genius rate-limit window to drain (zero traffic), probe
once per cycle, and run the full enricher the moment Genius answers 200.
Designed to run detached in the background."""
import time, urllib.request, urllib.parse, subprocess, sys, os

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
HERE = os.path.dirname(os.path.abspath(__file__))

def probe():
    try:
        req = urllib.request.Request('https://genius.com/api/search/multi?q=' + urllib.parse.quote('test'), headers=UA)
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return f"ERR:{type(e).__name__}"

WAIT = 1500  # 25 min of silence between probes
for cycle in range(8):  # up to ~3.3h
    print(f"[{time.strftime('%H:%M:%S')}] cycle {cycle}: cooling {WAIT}s (silent)...", flush=True)
    time.sleep(WAIT)
    code = probe()
    print(f"[{time.strftime('%H:%M:%S')}] cycle {cycle}: genius probe -> {code}", flush=True)
    if code == 200:
        print("Genius is reachable — running full enrichment.", flush=True)
        subprocess.run(['python3', '-u', os.path.join(HERE, 'enrich_app_catalog.py')], cwd=HERE)
        print("ENRICHMENT_COMPLETE", flush=True)
        sys.exit(0)
print("Gave up after all cycles — Genius still banned.", flush=True)
sys.exit(1)
