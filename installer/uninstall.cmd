@echo off
setlocal

set "APP_NAME=ISIR Kontrola"
set "INSTALL_DIR=%LOCALAPPDATA%\ISIR-Kontrola"
set "EXE_NAME=ISIR-Kontrola.exe"
set "DESKTOP_LINK=%USERPROFILE%\Desktop\ISIR Kontrola.lnk"
set "START_MENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "START_LINK=%START_MENU%\ISIR Kontrola.lnk"
set "UNINSTALL_LINK=%START_MENU%\Odinstalovat ISIR Kontrola.lnk"
set "RESET_LINK=%START_MENU%\Vymazat data ISIR Kontrola.lnk"
set "STARTUP_LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ISIR Kontrola.lnk"

echo Odinstalace aplikace %APP_NAME%
echo.
echo Aplikace je nainstalovana zde:
echo %INSTALL_DIR%
echo.
choice /C AN /N /M "Smazat klienty a vsechna data aplikace? [A/N] "
set "DELETE_DATA=%ERRORLEVEL%"
set "SCRIPT_DIR=%~dp0"

taskkill /IM "%EXE_NAME%" /F >nul 2>nul

del "%DESKTOP_LINK%" >nul 2>nul
del "%START_LINK%" >nul 2>nul
del "%UNINSTALL_LINK%" >nul 2>nul
del "%RESET_LINK%" >nul 2>nul
del "%STARTUP_LINK%" >nul 2>nul

if "%DELETE_DATA%"=="2" (
    echo.
    echo Data zustanou zachovana ve slozce:
    echo %INSTALL_DIR%
    start "" /D "%TEMP%" powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 2; Remove-Item -LiteralPath '%INSTALL_DIR%\%EXE_NAME%','%INSTALL_DIR%\README.txt','%INSTALL_DIR%\uninstall.cmd','%INSTALL_DIR%\reset_data.cmd' -Force -ErrorAction SilentlyContinue"
) else (
    echo.
    echo Mazani aplikace vcetne klientu a dat...
    start "" /D "%TEMP%" powershell -NoProfile -ExecutionPolicy Bypass -Command "$installDir='%INSTALL_DIR%'; $scriptDir='%SCRIPT_DIR%'; $exe='%EXE_NAME%'; Set-Location $env:TEMP; Stop-Process -Name ([IO.Path]::GetFileNameWithoutExtension($exe)) -Force -ErrorAction SilentlyContinue; Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($installDir, [System.StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force -ErrorAction SilentlyContinue; $targets = @($installDir, (Join-Path $scriptDir 'data')) | Select-Object -Unique; foreach ($target in $targets) { for ($i=0; $i -lt 30 -and (Test-Path -LiteralPath $target); $i++) { Start-Sleep -Milliseconds 500; Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue } }; $remaining = $targets | Where-Object { Test-Path -LiteralPath $_ }; if ($remaining) { $joined = ($remaining | ForEach-Object { 'Remove-Item -LiteralPath ""' + $_ + '"" -Recurse -Force -ErrorAction SilentlyContinue' }) -join '; '; Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command', ('Start-Sleep -Seconds 3; ' + $joined) -WindowStyle Hidden }"
)

echo.
echo Odinstalace byla spustena. Toto okno muzete zavrit.
endlocal
