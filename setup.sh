#!/bin/bash
set -e

echo "Setting up workspace..."
mkdir -p /Users/dime/Desktop/DIME_STEM_PIPELINE/tracks /Users/dime/Desktop/DIME_STEM_PIPELINE/stems
cp /Users/dime/Developer/scrappadata/tracklist.txt /Users/dime/Desktop/DIME_STEM_PIPELINE/tracklist.txt

echo "Setting up Python virtual environment..."
cd /Users/dime/Desktop/DIME_STEM_PIPELINE
python3 -m venv venv
source venv/bin/activate

echo "Installing yt-dlp and demucs..."
pip install yt-dlp==2024.07.09 demucs

echo "Setup complete."
