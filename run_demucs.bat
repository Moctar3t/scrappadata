@echo off
cd /d "%USERPROFILE%\Desktop\DIME_STEM_PIPELINE"
echo ===================================================
echo 📥 DIME STEM PIPELINE - GPU ACCELERATED DEMUCS
echo ===================================================
echo.
echo Launching GPU accelerated separation wrapper...
"%USERPROFILE%\demucs_relay\venv\Scripts\python.exe" run_demucs.py
echo.
echo ===================================================
pause
