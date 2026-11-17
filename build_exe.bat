@echo off
REM Builds a single-file dist\MedusaPDF.exe with icon (no console window)
setlocal
cd /d "%~dp0"

echo Installing build requirements...
python -m pip install -r requirements.txt pyinstaller --disable-pip-version-check
if errorlevel 1 goto :fail

echo Building MedusaPDF.exe...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name MedusaPDF ^
  --icon "assets\medusa.ico" ^
  --add-data "assets;assets" ^
  --add-data "scripts;scripts" ^
  --collect-all pdf2docx ^
  --collect-all fitz ^
  --collect-all pymupdf ^
  main.py
if errorlevel 1 goto :fail

echo.
echo Done:  dist\MedusaPDF.exe
echo Tip: if "Start with Windows" was enabled while running from source,
echo      open the EXE once - it refreshes the startup entry automatically.
pause
exit /b 0

:fail
echo.
echo BUILD FAILED
pause
exit /b 1
