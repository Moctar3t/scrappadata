cd C:\Users\Dimé\demucs_relay
call venv\Scripts\activate.bat
python -u worker.py >> worker.log 2>&1
