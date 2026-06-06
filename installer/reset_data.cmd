@echo off
setlocal

set "INSTALL_DIR=%LOCALAPPDATA%\ISIR-Kontrola"

echo Mazani starych dat aplikace...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Remove-Item -LiteralPath '%INSTALL_DIR%\data','%INSTALL_DIR%\downloaded_documents' -Recurse -Force -ErrorAction SilentlyContinue"

echo Hotovo. Pri pristim spusteni bude aplikace prazdna.
endlocal
