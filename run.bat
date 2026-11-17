@echo off
REM Runs MedusaPDF from source (installs requirements first time)
cd /d "%~dp0"
python -m pip install -r requirements.txt --quiet --disable-pip-version-check
start "" pythonw main.py
