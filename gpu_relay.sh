#!/bin/bash
# GPU Relay Script: Mac -> Windows GPU PC
# ----------------------------------------

# Configuration
REMOTE_IP="192.168.1.65"
REMOTE_USER="dimé"
# Using relative paths to avoid Windows drive-letter/encoding headaches
REMOTE_WORKSPACE="demucs_relay"
REMOTE_DEMUCS="demucs_relay/venv/Scripts/demucs.exe"
LOCAL_TRACKS="/Users/dime/Desktop/DIME_STEM_PIPELINE.nosync/tracks/"
LOCAL_STEMS="/Users/dime/Desktop/DIME_STEM_PIPELINE.nosync/stems/"

echo "🚀 Starting GPU Relay Pipeline..."

# 1. Prepare & Sync Up: Mac -> PC
echo "📦 Compressing tracks for safe transport (excluding Mac metadata)..."
COPYFILE_DISABLE=1 tar -czf tracks.tar.gz -C "$LOCAL_TRACKS" --exclude='._*' .

echo "📤 Uploading archive to GPU PC..."
scp -i ~/.ssh/id_ed25519_gpu tracks.tar.gz "$REMOTE_USER@$REMOTE_IP:demucs_relay/"

echo "📦 Extracting tracks on GPU PC..."
ssh -i ~/.ssh/id_ed25519_gpu "$REMOTE_USER@$REMOTE_IP" "cd demucs_relay && tar -xzf tracks.tar.gz -C tracks/ && powershell -Command \"(Get-ChildItem tracks/*.mp3).Count | Out-File -FilePath upload_count.txt\""

echo "✅ Verify: $(ssh -i ~/.ssh/id_ed25519_gpu "$REMOTE_USER@$REMOTE_IP" "cat demucs_relay/upload_count.txt") tracks received on GPU PC."

# 2. Remote Execute: Run Demucs on GPU
echo "🧠 Triggering GPU-accelerated Demucs separation..."
# We skip macOS "._" metadata files and set UTF-8 encoding for Unicode safety
ssh -i ~/.ssh/id_ed25519_gpu "$REMOTE_USER@$REMOTE_IP" "powershell -Command \"\$env:PYTHONIOENCODING='utf-8'; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-ChildItem 'demucs_relay/tracks/*.mp3' | Where-Object { \$_.Name -notlike '._*' } | ForEach-Object { Write-Host 'Processing: ' \$_.Name; & 'demucs_relay/venv/Scripts/demucs.exe' --mp3 --mp3-bitrate 192 -n htdemucs_ft \$_.FullName -o 'demucs_relay/stems' }\""

# 3. Sync Down: PC -> Mac
echo "📥 Syncing isolated stems back to Mac..."
# We use rsync here as the stem folders are simpler, or we could zip back too
rsync -avz --progress -e "ssh -i $HOME/.ssh/id_ed25519_gpu" "$REMOTE_USER@$REMOTE_IP:demucs_relay/stems/" "$LOCAL_STEMS"

# Cleanup local temp archive
rm tracks.tar.gz

echo "✅ Relay Pipeline Complete! Your high-quality stems are now in $LOCAL_STEMS"
