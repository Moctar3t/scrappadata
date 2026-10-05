@echo off
echo ===================================================
echo 📊 DIME STEM PIPELINE - METADATA SCRAPER
echo ===================================================
echo.
echo Analyzing BPM, Musical Key, Year and Cover Art...
C:\Users\Dimé\demucs_relay\venv\Scripts\python.exe metadata_scraper.py
echo.
echo ✅ Done! Outputs generated:
echo - metadata_cards.json
echo - metadata_records.json
echo ===================================================
pause
