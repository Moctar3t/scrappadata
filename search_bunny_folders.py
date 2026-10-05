import os
import sys
import urllib.request
import json
import urllib.parse

STORAGE_ZONE = "videobb"
# The storage zone key comes from the environment, never from this file.
ACCESS_KEY = os.environ.get("BUNNY_STORAGE_PASSWORD") or sys.exit("set BUNNY_STORAGE_PASSWORD")
BASE_URL = f"https://storage.bunnycdn.com/{STORAGE_ZONE}/"

def list_root_folders():
    req = urllib.request.Request(
        BASE_URL,
        headers={"AccessKey": ACCESS_KEY, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15.0) as response:
            items = json.loads(response.read().decode('utf-8'))
            
            # Print unique directory names in the root
            folders = sorted(list(set(item['ObjectName'] for item in items if item['IsDirectory'])))
            print(f"Total folders found in CDN root: {len(folders)}")
            
            print("\nMatching folders for 'Celeste':")
            for f in folders:
                if 'Celeste' in f or 'celeste' in f:
                    print(f" - {f}")
                    
            print("\nMatching folders for 'Deus' / 'Shemar' / 'Brinx':")
            for f in folders:
                if any(x in f for x in ['Deus', 'Machina', 'Shemar', 'Brinx', 'deus', 'machina', 'shemar', 'brinx']):
                    print(f" - {f}")
                    
            print("\nMatching folders for 'Right Now' / 'Playboi' / 'Carti':")
            for f in folders:
                if any(x in f for x in ['Right Now', 'Playboi', 'Carti', 'right now', 'playboi', 'carti']):
                    print(f" - {f}")
                    
    except Exception as e:
        print(f"Error querying BunnyCDN: {e}")

if __name__ == "__main__":
    list_root_folders()
