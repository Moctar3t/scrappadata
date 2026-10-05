import csv
import os

csv_path = "/Users/dime/Downloads/Liked_Songs.csv"
output_path = "/Users/dime/Desktop/liked_songs_metadata_audit.md"

if not os.path.exists(csv_path):
    print(f"Error: Liked_Songs.csv not found at {csv_path}")
    exit(1)

# Pitch class mapping (Spotify standard)
KEY_MAP = {
    0: "C", 1: "C#", 2: "D", 3: "D#", 4: "E", 5: "F",
    6: "F#", 7: "G", 8: "G#", 9: "A", 10: "A#", 11: "B"
}

def format_duration(ms_str):
    try:
        ms = int(ms_str)
        total_seconds = ms // 1000
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes}:{seconds:02d}"
    except:
        return "0:00"

def get_musical_key(key_str, mode_str):
    try:
        key_num = int(float(key_str))
        mode_num = int(float(mode_str))
        key_name = KEY_MAP.get(key_num, "Unknown")
        mode_name = "Major" if mode_num == 1 else "Minor"
        return f"{key_name} {mode_name}"
    except:
        return "Unknown"

def get_bpm(tempo_str):
    try:
        return str(round(float(tempo_str)))
    except:
        return "Unknown"

print(f"Parsing {csv_path}...")

with open(csv_path, mode='r', encoding='utf-8', errors='ignore') as infile:
    reader = csv.reader(infile)
    header = next(reader)
    
    # Map header to indexes
    col_map = {col: idx for idx, col in enumerate(header)}
    
    rows_written = 0
    with open(output_path, mode='w', encoding='utf-8') as outfile:
        outfile.write("# DIME SYSTEMS - Liked Songs Core Catalog Audit\n")
        outfile.write(f"## 100% Authentic Metadata from Spotify Database ({csv_path})\n\n")
        
        # Write table headers
        outfile.write("| # | Track Name | Artist Name(s) | Release Date | Key | BPM | Duration | Record Label |\n")
        outfile.write("|---|---|---|---|---|---|---|---|\n")
        
        for idx, row in enumerate(reader, start=1):
            if not row:
                continue
            
            # Extract fields safely using header index
            name = row[col_map["Track Name"]]
            artists = row[col_map["Artist Name(s)"]]
            release_date = row[col_map["Release Date"]]
            duration_ms = row[col_map["Duration (ms)"]]
            key = row[col_map["Key"]]
            mode = row[col_map["Mode"]]
            tempo = row[col_map["Tempo"]]
            label = row[col_map["Record Label"]]
            
            # Format attributes
            fmt_key = get_musical_key(key, mode)
            fmt_bpm = get_bpm(tempo)
            fmt_duration = format_duration(duration_ms)
            
            # Escape markdown table characters
            esc_name = name.replace("|", "\\|")
            esc_artists = artists.replace("|", "\\|")
            esc_label = label.replace("|", "\\|")
            
            outfile.write(f"| {idx} | **{esc_name}** | {esc_artists} | {release_date} | {fmt_key} | {fmt_bpm} | {fmt_duration} | {esc_label} |\n")
            rows_written += 1

print(f"Successfully processed {rows_written} tracks and wrote audit to {output_path}!")
