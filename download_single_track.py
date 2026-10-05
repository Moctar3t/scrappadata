import os
import yt_dlp

tracks_dir = "C:\\Users\\Dimé\\Desktop\\DIME_STEM_PIPELINE\\tracks"
artist = "Drake"
title = "Janice (SFTU)"
track_name_formatted = f"{artist} - {title}"

if not os.path.exists(tracks_dir):
    os.makedirs(tracks_dir)

ydl_opts = {
    'format': 'bestaudio/best',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
    'quiet': False,
    'outtmpl': os.path.join(tracks_dir, f"{track_name_formatted}.%(ext)s"),
    'extractor_args': {
        'youtube': {
            'player_client': ['android'],
        }
    }
}

print(f"Downloading {track_name_formatted} from YouTube search...")
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    ydl.download([f"ytsearch1:drake janice stfu"])
print("Download complete!")
