@echo off
setlocal

cd /d "%~dp0"

if not exist "dist\ISIR-Kontrola.exe" (
    call build_exe.bat
)

if not exist "installer_build" mkdir "installer_build"
copy /Y "dist\ISIR-Kontrola.exe" "installer_build\ISIR-Kontrola.exe" >nul
copy /Y "installer\install.cmd" "installer_build\install.cmd" >nul
copy /Y "installer\uninstall.cmd" "installer_build\uninstall.cmd" >nul
copy /Y "installer\reset_data.cmd" "installer_build\reset_data.cmd" >nul
copy /Y "installer\README-installer.txt" "installer_build\README-installer.txt" >nul

iexpress /N /Q "installer\ISIR-Kontrola.sed"

echo.
echo Hotovo. Instalator najdete jako ISIR-Kontrola-Setup.exe
pause
