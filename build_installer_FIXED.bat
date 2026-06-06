@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "APP_EXE=dist\ISIR-Kontrola.exe"
set "INSTALLER_DIR=installer"
set "PAYLOAD_DIR=%CD%\installer_build"
set "SED_FILE=%CD%\installer\ISIR-Kontrola.sed"
set "SETUP_EXE=%CD%\ISIR-Kontrola-Setup.exe"

if not exist "%APP_EXE%" (
    echo Soubor %APP_EXE% neexistuje. Spoustim build_exe.bat...
    call build_exe.bat
    if errorlevel 1 (
        echo.
        echo CHYBA: build_exe.bat skoncil chybou.
        pause
        exit /b 1
    )
)

if not exist "%APP_EXE%" (
    echo.
    echo CHYBA: Po spusteni build_exe.bat stale neexistuje %APP_EXE%.
    pause
    exit /b 1
)

for %%F in (
    "%INSTALLER_DIR%\install.cmd"
    "%INSTALLER_DIR%\uninstall.cmd"
    "%INSTALLER_DIR%\reset_data.cmd"
    "%INSTALLER_DIR%\README-installer.txt"
) do (
    if not exist %%~F (
        echo.
        echo CHYBA: Chybi soubor %%~F
        pause
        exit /b 1
    )
)

if exist "%PAYLOAD_DIR%" rmdir /S /Q "%PAYLOAD_DIR%"
mkdir "%PAYLOAD_DIR%"
if errorlevel 1 (
    echo.
    echo CHYBA: Nepodarilo se vytvorit slozku %PAYLOAD_DIR%.
    pause
    exit /b 1
)

copy /Y "%APP_EXE%" "%PAYLOAD_DIR%\ISIR-Kontrola.exe" >nul || goto copy_error
copy /Y "%INSTALLER_DIR%\install.cmd" "%PAYLOAD_DIR%\install.cmd" >nul || goto copy_error
copy /Y "%INSTALLER_DIR%\uninstall.cmd" "%PAYLOAD_DIR%\uninstall.cmd" >nul || goto copy_error
copy /Y "%INSTALLER_DIR%\reset_data.cmd" "%PAYLOAD_DIR%\reset_data.cmd" >nul || goto copy_error
copy /Y "%INSTALLER_DIR%\README-installer.txt" "%PAYLOAD_DIR%\README-installer.txt" >nul || goto copy_error

goto after_copy

:copy_error
echo.
echo CHYBA: Nepodarilo se zkopirovat soubory do installer_build.
pause
exit /b 1

:after_copy

rem Vytvor aktualni SED soubor s cestami podle aktualni slozky projektu.
> "%SED_FILE%" (
    echo [Version]
    echo Class=IEXPRESS
    echo SEDVersion=3
    echo [Options]
    echo PackagePurpose=InstallApp
    echo ShowInstallProgramWindow=1
    echo HideExtractAnimation=1
    echo UseLongFileName=1
    echo InsideCompressed=1
    echo CAB_FixedSize=0
    echo CAB_ResvCodeSigning=0
    echo RebootMode=N
    echo InstallPrompt=
    echo DisplayLicense=
    echo FinishMessage=Instalace aplikace ISIR Kontrola byla dokoncena.
    echo TargetName=%SETUP_EXE%
    echo FriendlyName=ISIR Kontrola
    echo AppLaunched=install.cmd
    echo PostInstallCmd=^<None^>
    echo AdminQuietInstCmd=
    echo UserQuietInstCmd=
    echo SourceFiles=SourceFiles
    echo [Strings]
    echo FILE0="ISIR-Kontrola.exe"
    echo FILE1="install.cmd"
    echo FILE2="README-installer.txt"
    echo FILE3="uninstall.cmd"
    echo FILE4="reset_data.cmd"
    echo [SourceFiles]
    echo SourceFiles0=%PAYLOAD_DIR%\
    echo [SourceFiles0]
    echo %%FILE0%%=
    echo %%FILE1%%=
    echo %%FILE2%%=
    echo %%FILE3%%=
    echo %%FILE4%%=
)

if exist "%SETUP_EXE%" del /F /Q "%SETUP_EXE%"

iexpress /N "%SED_FILE%"

if errorlevel 1 (
    echo.
    echo CHYBA: IExpress skoncil chybou.
    echo SED soubor: %SED_FILE%
    pause
    exit /b 1
)

if not exist "%SETUP_EXE%" (
    echo.
    echo CHYBA: Instalator nebyl vytvoren.
    echo Ocekavana cesta:
    echo %SETUP_EXE%
    echo.
    echo Zkontrolujte vystup IExpressu vyse.
    pause
    exit /b 1
)

echo.
echo Hotovo. Instalator byl vytvoren zde:
echo %SETUP_EXE%
pause
exit /b 0
