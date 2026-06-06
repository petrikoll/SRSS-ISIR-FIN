@echo off
setlocal

set "APP_NAME=ISIR Kontrola"
set "INSTALL_DIR=%LOCALAPPDATA%\ISIR-Kontrola"
set "EXE_NAME=ISIR-Kontrola.exe"
set "UNINSTALL_NAME=uninstall.cmd"
set "RESET_NAME=reset_data.cmd"
set "INSTALL_LOG=%INSTALL_DIR%\install.log"

echo Instalace aplikace %APP_NAME%
echo Cesta instalace: %INSTALL_DIR%
echo.
echo Ukoncuji pripadne bezici instance...
taskkill /IM "%EXE_NAME%" /F >nul 2>nul
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
echo [%DATE% %TIME%] Start instalace > "%INSTALL_LOG%"
echo [%DATE% %TIME%] Cesta instalace: %INSTALL_DIR% >> "%INSTALL_LOG%"
echo Kopiruji soubory aplikace...
copy /Y "%~dp0%EXE_NAME%" "%INSTALL_DIR%\%EXE_NAME%" >nul
copy /Y "%~dp0%UNINSTALL_NAME%" "%INSTALL_DIR%\%UNINSTALL_NAME%" >nul
copy /Y "%~dp0%RESET_NAME%" "%INSTALL_DIR%\%RESET_NAME%" >nul
copy /Y "%~dp0README-installer.txt" "%INSTALL_DIR%\README.txt" >nul
echo [%DATE% %TIME%] Soubory zkopirovany >> "%INSTALL_LOG%"

echo Vytvarim zastupce...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws=New-Object -ComObject WScript.Shell; $installDir=Join-Path $env:LOCALAPPDATA 'ISIR-Kontrola'; $target=Join-Path $installDir 'ISIR-Kontrola.exe'; $desktop=[Environment]::GetFolderPath('Desktop'); $lnk=$ws.CreateShortcut((Join-Path $desktop 'ISIR Kontrola.lnk')); $lnk.TargetPath=$target; $lnk.WorkingDirectory=$installDir; $lnk.Save(); $startMenu=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'; $lnk2=$ws.CreateShortcut((Join-Path $startMenu 'ISIR Kontrola.lnk')); $lnk2.TargetPath=$target; $lnk2.WorkingDirectory=$installDir; $lnk2.Save(); $uninstall=Join-Path $installDir 'uninstall.cmd'; $lnk3=$ws.CreateShortcut((Join-Path $startMenu 'Odinstalovat ISIR Kontrola.lnk')); $lnk3.TargetPath=$env:ComSpec; $lnk3.Arguments='/c \"\"' + $uninstall + '\"\"'; $lnk3.WorkingDirectory=$env:TEMP; $lnk3.Save(); $reset=Join-Path $installDir 'reset_data.cmd'; $lnk4=$ws.CreateShortcut((Join-Path $startMenu 'Vymazat data ISIR Kontrola.lnk')); $lnk4.TargetPath=$env:ComSpec; $lnk4.Arguments='/c \"\"' + $reset + '\"\"'; $lnk4.WorkingDirectory=$env:TEMP; $lnk4.Save()"
if errorlevel 1 echo [%DATE% %TIME%] Chyba pri vytvareni zastupcu >> "%INSTALL_LOG%"

echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; $result=[System.Windows.Forms.MessageBox]::Show('Spoustet ISIR Kontrola automaticky po startu Windows?','ISIR Kontrola',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Question); $ws=New-Object -ComObject WScript.Shell; $installDir=Join-Path $env:LOCALAPPDATA 'ISIR-Kontrola'; $target=Join-Path $installDir 'ISIR-Kontrola.exe'; $startup=[Environment]::GetFolderPath('Startup'); $link=Join-Path $startup 'ISIR Kontrola.lnk'; if ($result -eq [System.Windows.Forms.DialogResult]::Yes) { $lnk=$ws.CreateShortcut($link); $lnk.TargetPath=$target; $lnk.WorkingDirectory=$installDir; $lnk.Save(); Add-Content -LiteralPath (Join-Path $installDir 'install.log') -Value ('[{0}] Automaticke spousteni: Ano' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) } else { Remove-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue; Add-Content -LiteralPath (Join-Path $installDir 'install.log') -Value ('[{0}] Automaticke spousteni: Ne' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) }"
if errorlevel 1 echo [%DATE% %TIME%] Chyba pri nastaveni automatickeho spousteni >> "%INSTALL_LOG%"

echo.
echo Spoustim aplikaci. Pokud se okno prohlizece neotevre, spustte zastupce ISIR Kontrola na plose.
echo Diagnostika startu bude ulozena zde: %INSTALL_DIR%\startup.log
start "" "%INSTALL_DIR%\%EXE_NAME%"
echo [%DATE% %TIME%] Aplikace spustena >> "%INSTALL_LOG%"
timeout /T 3 /NOBREAK >nul
echo.
echo Instalace byla dokoncena.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('Instalace ISIR Kontrola byla dokoncena. Aplikace se spousti v prohlizeci. Pokud se neotevre, pouzijte zastupce ISIR Kontrola na plose.','ISIR Kontrola',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null"

endlocal
