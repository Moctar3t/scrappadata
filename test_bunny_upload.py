import urllib.request
import urllib.error

# The key from scripts/.env (BUNNY_STORAGE_PASSWORD), as the ingest reads it.
from ingest_gpu import STORAGE_KEY

url = "https://storage.bunnycdn.com/videobb/test_dir/test.txt"
headers = {
    "AccessKey": STORAGE_KEY,
    "Content-Type": "application/octet-stream"
}
data = b"hello world"

print("Sending PUT to test BunnyCDN upload...")
req = urllib.request.Request(url, data=data, headers=headers, method='PUT')
try:
    with urllib.request.urlopen(req) as response:
        print(f"Status: {response.status}")
        print(f"Response: {response.read().decode()}")
except urllib.error.HTTPError as e:
    print(f"HTTPError: {e.code} - {e.reason}")
    print(f"Headers: {e.headers}")
    try:
        print(f"Body: {e.read().decode()}")
    except:
        pass
except Exception as e:
    print(f"Error: {e}")
