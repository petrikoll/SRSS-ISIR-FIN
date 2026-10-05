@echo off
setlocal
echo Odinstalace programu ISIR Kontrola. Klienti, dokumenty a zalohy zustanou zachovany.
echo Datove uloziste: %LOCALAPPDATA%\ISIR-Kontrola
choice /C AN /N /M "Odinstalovat program? [A/N] "
if not "%ERRORLEVEL%"=="1" exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -Command "$appRoot=[IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'ISIR-Kontrola')); $exe=Join-Path $appRoot 'ISIR-Kontrola.exe'; Get-Process | Where-Object { $_.Path -and $_.Path.Equals($exe,[StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force -ErrorAction Stop; $menus=@([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'),[Environment]::GetFolderPath('Startup')); foreach($menu in $menus){ if($menu){ foreach($name in @('ISIR Kontrola.lnk','Odinstalovat ISIR Kontrola.lnk','Vymazat data ISIR Kontrola.lnk')){ Remove-Item -LiteralPath (Join-Path $menu $name) -Force -ErrorAction SilentlyContinue } } }; foreach($name in @('ISIR-Kontrola.exe','README.txt','reset_data.cmd')){ $target=Join-Path $appRoot $name; if(Test-Path -LiteralPath $target){ Remove-Item -LiteralPath $target -Force -ErrorAction Stop } }"
if errorlevel 1 (
    echo Odinstalace nebyla dokoncena. Ukoncete aplikaci a zkuste to znovu.
    pause
    exit /b 1
)
echo Program byl odstranen. Klientska data a zalohy zustaly zachovany.
pause
endlocal
