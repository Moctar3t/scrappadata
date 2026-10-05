import subprocess
import time
import os
import re

UPLOADER_EXP = "/Users/dime/Developer/scrappadata/run_uploader_win.exp"
GET_TRACKS_EXP = "/Users/dime/Developer/scrappadata/get_all_separated_tracks.exp"
RAW_TXT = "/Users/dime/Developer/scrappadata/separated_raw.txt"
GEN_STEMS_PY = "/Users/dime/Developer/scrappadata/generate_stems_list.py"
SMART_LINK_PY = "/Users/dime/Developer/scrappadata/smart_link_stems.py"

def run_command(cmd, shell=False, redirect_to=None):
    if redirect_to:
        with open(redirect_to, 'w', encoding='utf-8') as f:
            res = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, shell=shell)
    else:
        res = subprocess.run(cmd, capture_output=True, text=True, shell=shell)
    return res

def check_status_remote():
    # Run the fast status check expect script
    cmd = ["expect", "/Users/dime/Developer/scrappadata/run_status_check.exp"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    output = res.stdout
    
    completed = None
    remaining = None
    
    # Try to find "COMPLETED:X" and "REMAINING:Y"
    m_comp = re.search(r'COMPLETED:(\d+)', output)
    m_rem = re.search(r'REMAINING:(\d+)', output)
    
    if m_comp:
        completed = int(m_comp.group(1))
    if m_rem:
        remaining = int(m_rem.group(1))
        
    return completed, remaining, output

def main():
    print("=== DIME SYSTEMS - REAL-TIME CATALOG SYNC LOOP ===")
    
    while True:
        print("\nChecking demucs pipeline status on remote GPU PC...")
        completed, remaining, raw_output = check_status_remote()
        
        if completed is not None and remaining is not None:
            print(f"📍 Demucs Progress: {completed} completed, {remaining} remaining.")
        else:
            print("⚠️ Warning: Could not parse demucs progress from remote. Raw output:")
            print(raw_output)
            remaining = 999  # Safe default to keep loop going
            
        if remaining == 0:
            print("🎉 Demucs pipeline finished! Let's do one last sync run and exit.")
            
        print("1. Running Uploader to sync new stems to Bunny CDN...")
        res = run_command(["expect", UPLOADER_EXP])
        # Find newly uploaded count
        m_new = re.search(r'Newly Uploaded:\s*(\d+)', res.stdout)
        if m_new:
            print(f"   -> Newly uploaded stems: {m_new.group(1)}")
        else:
            print("   -> Uploader complete.")
            
        print("2. Fetching list of all completed track folders from remote...")
        run_command(["expect", GET_TRACKS_EXP], redirect_to=RAW_TXT)
        
        print("3. Generating stems list markdown...")
        run_command(["python3", GEN_STEMS_PY])
        
        print("4. Updating catalog JSON and rebuilding music catalog...")
        res = run_command(["python3", SMART_LINK_PY])
        # Find matched and updated count
        m_match = re.search(r'Matched\s*(\d+)\s*/\s*(\d+)', res.stdout)
        m_upd = re.search(r'Updated stems arrays in (\d+)', res.stdout)
        m_idx = re.search(r'\[build-catalog\].*', res.stdout)
        
        if m_match and m_upd:
            print(f"   -> Matched {m_match.group(1)} tracks, updated {m_upd.group(1)} JSON files.")
        if m_idx:
            print(f"   -> {m_idx.group(0)}")
            
        if remaining == 0:
            print("\n✅ Sync Loop Finished successfully! All tracks are completed, uploaded, and indexed.")
            break
            
        print("Waiting 45 seconds before next sync tick...")
        time.sleep(45)

if __name__ == "__main__":
    main()
