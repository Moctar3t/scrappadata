#!/bin/bash
set -e

WORKSPACE="/Users/dime/Desktop/DIME_STEM_PIPELINE.nosync"
cd "$WORKSPACE"

source venv/bin/activate
export PATH="$WORKSPACE/venv/bin:$PATH"

echo "Starting Demucs separation on all tracks..."
# Use python3 -m demucs.separate to bypass shebang issues from the folder rename
venv/bin/python3 -m demucs.separate --mp3 --mp3-bitrate 192 -n htdemucs_ft tracks/*.mp3 -o stems
echo "Demucs separation completed."
