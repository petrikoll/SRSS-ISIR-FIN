@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
".venv\Scripts\python.exe" -m pip install pyinstaller

".venv\Scripts\python.exe" -m PyInstaller ^
  --onefile ^
  --noconsole ^
  --name ISIR-Kontrola ^
  --icon "isir.ico" ^
  --add-data "templates;templates" ^
  --add-data "drdd_upscaled.jpg;." ^
  start_app.py

echo.
echo Hotovo. EXE najdete ve slozce dist\ISIR-Kontrola.exe
pause
