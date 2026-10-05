import subprocess
import json

def fetch_remote_file():
    # Run ssh type via expect
    cmd = [
        "expect", "-c",
        'set timeout 15; spawn ssh -i ~/.ssh/id_ed25519_gpu "dimé@192.168.1.64" "cmd /c type C:\\\\Users\\\\Dimé\\\\Desktop\\\\DIME_STEM_PIPELINE\\\\reencode_progress.json"; expect eof'
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    
    stdout = res.stdout
    # Locate the start of JSON
    start_idx = stdout.find("{")
    end_idx = stdout.rfind("}")
    
    if start_idx != -1 and end_idx != -1:
        json_str = stdout[start_idx:end_idx+1]
        try:
            data = json.loads(json_str)
            return data
        except Exception as e:
            print(f"Failed to parse JSON: {e}")
            print("Extracted string was:")
            print(json_str)
    else:
        print("JSON braces not found in stdout:")
        print(stdout)
    return None

def main():
    data = fetch_remote_file()
    if data:
        print("Successfully read progress data remotely!")
        print(json.dumps(data, indent=2))

if __name__ == "__main__":
    main()
