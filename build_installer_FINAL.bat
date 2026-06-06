@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM  ISIR Kontrola - tvorba instalacniho EXE pres IExpress
REM  Tento BAT musi lezet v hlavni slozce projektu.
REM ============================================================

cd /d "%~dp0"

set "PROJECT_DIR=%cd%"
set "APP_EXE=%PROJECT_DIR%\dist\ISIR-Kontrola.exe"
set "INSTALLER_DIR=%PROJECT_DIR%\installer"
set "BUILD_DIR=%PROJECT_DIR%\installer_build"
set "SED_FILE=%INSTALLER_DIR%\ISIR-Kontrola.sed"
set "SETUP_EXE=%PROJECT_DIR%\ISIR-Kontrola-Setup.exe"

echo.
echo ============================================================
echo  ISIR Kontrola - build instalatoru
echo ============================================================
echo Projekt:        %PROJECT_DIR%
echo Aplikace EXE:   %APP_EXE%
echo Installer dir:  %INSTALLER_DIR%
echo Build dir:      %BUILD_DIR%
echo SED:            %SED_FILE%
echo Vystup:         %SETUP_EXE%
echo.

REM 1) Kontrola, ze existuje aktualni aplikacni EXE
if not exist "%APP_EXE%" (
    echo CHYBA: Nenalezeno aplikacni EXE:
    echo %APP_EXE%
    echo.
    echo Nejdive spustte build_exe.bat a overte, ze vzniklo dist\ISIR-Kontrola.exe
    echo.
    pause
    exit /b 1
)

REM 2) Kontrola installer slozky
if not exist "%INSTALLER_DIR%" (
    echo CHYBA: Nenalezena slozka installer:
    echo %INSTALLER_DIR%
    echo.
    pause
    exit /b 1
)

REM 3) Kontrola pomocnych souboru
if not exist "%INSTALLER_DIR%\install.cmd" (
    echo CHYBA: Chybi installer\install.cmd
    pause
    exit /b 1
)

if not exist "%INSTALLER_DIR%\uninstall.cmd" (
    echo CHYBA: Chybi installer\uninstall.cmd
    pause
    exit /b 1
)

if not exist "%INSTALLER_DIR%\reset_data.cmd" (
    echo CHYBA: Chybi installer\reset_data.cmd
    pause
    exit /b 1
)

if not exist "%INSTALLER_DIR%\README-installer.txt" (
    echo CHYBA: Chybi installer\README-installer.txt
    pause
    exit /b 1
)

REM 4) Znovu vytvoreni installer_build
echo Pripravuji installer_build...
if exist "%BUILD_DIR%" (
    rmdir /s /q "%BUILD_DIR%"
)
mkdir "%BUILD_DIR%"
if errorlevel 1 (
    echo CHYBA: Nepodarilo se vytvorit installer_build.
    pause
    exit /b 1
)

copy /y "%APP_EXE%" "%BUILD_DIR%\ISIR-Kontrola.exe" >nul
copy /y "%INSTALLER_DIR%\install.cmd" "%BUILD_DIR%\install.cmd" >nul
copy /y "%INSTALLER_DIR%\uninstall.cmd" "%BUILD_DIR%\uninstall.cmd" >nul
copy /y "%INSTALLER_DIR%\reset_data.cmd" "%BUILD_DIR%\reset_data.cmd" >nul
copy /y "%INSTALLER_DIR%\README-installer.txt" "%BUILD_DIR%\README-installer.txt" >nul

echo Obsah installer_build:
dir "%BUILD_DIR%"
echo.

REM 5) Smazani stare instalacky v aktualni projektove slozce
if exist "%SETUP_EXE%" (
    echo Mazu starou instalacku:
    echo %SETUP_EXE%
    del /f /q "%SETUP_EXE%"
)

REM 6) Vytvoreni noveho SED souboru s aktualnimi cestami
echo Vytvarim novy SED soubor:
echo %SED_FILE%
echo.

> "%SED_FILE%" echo [Version]
>>"%SED_FILE%" echo Class=IEXPRESS
>>"%SED_FILE%" echo SEDVersion=3
>>"%SED_FILE%" echo [Options]
>>"%SED_FILE%" echo PackagePurpose=InstallApp
>>"%SED_FILE%" echo ShowInstallProgramWindow=0
>>"%SED_FILE%" echo HideExtractAnimation=1
>>"%SED_FILE%" echo UseLongFileName=1
>>"%SED_FILE%" echo InsideCompressed=0
>>"%SED_FILE%" echo CAB_FixedSize=0
>>"%SED_FILE%" echo CAB_ResvCodeSigning=0
>>"%SED_FILE%" echo RebootMode=N
>>"%SED_FILE%" echo InstallPrompt=
>>"%SED_FILE%" echo DisplayLicense=
>>"%SED_FILE%" echo FinishMessage=
>>"%SED_FILE%" echo TargetName=%SETUP_EXE%
>>"%SED_FILE%" echo FriendlyName=ISIR Kontrola
>>"%SED_FILE%" echo AppLaunched=install.cmd
>>"%SED_FILE%" echo PostInstallCmd=^<None^>
>>"%SED_FILE%" echo AdminQuietInstCmd=
>>"%SED_FILE%" echo UserQuietInstCmd=
>>"%SED_FILE%" echo SourceFiles=SourceFiles
>>"%SED_FILE%" echo [Strings]
>>"%SED_FILE%" echo FILE0="ISIR-Kontrola.exe"
>>"%SED_FILE%" echo FILE1="install.cmd"
>>"%SED_FILE%" echo FILE2="uninstall.cmd"
>>"%SED_FILE%" echo FILE3="reset_data.cmd"
>>"%SED_FILE%" echo FILE4="README-installer.txt"
>>"%SED_FILE%" echo [SourceFiles]
>>"%SED_FILE%" echo SourceFiles0=%BUILD_DIR%\
>>"%SED_FILE%" echo [SourceFiles0]
>>"%SED_FILE%" echo %%FILE0%%=
>>"%SED_FILE%" echo %%FILE1%%=
>>"%SED_FILE%" echo %%FILE2%%=
>>"%SED_FILE%" echo %%FILE3%%=
>>"%SED_FILE%" echo %%FILE4%%=

if not exist "%SED_FILE%" (
    echo CHYBA: SED soubor nebyl vytvoren.
    pause
    exit /b 1
)

echo Kontrola TargetName a SourceFiles:
findstr /i "TargetName SourceFiles0" "%SED_FILE%"
echo.

REM 7) Kontrola dostupnosti IExpress
where iexpress >nul 2>nul
if errorlevel 1 (
    echo CHYBA: IExpress nebyl nalezen v PATH.
    echo Obvykle by mel byt ve Windows dostupny jako:
    echo C:\Windows\System32\iexpress.exe
    echo.
    pause
    exit /b 1
)

REM 8) Spusteni IExpress
echo Spoustim IExpress...
iexpress /N "%SED_FILE%"
set "IEXPRESS_EXIT=%ERRORLEVEL%"

echo.
echo IExpress exit code: %IEXPRESS_EXIT%

if not "%IEXPRESS_EXIT%"=="0" (
    echo.
    echo CHYBA: IExpress skoncil chybou.
    echo Zkontrolujte vypis vyse.
    pause
    exit /b 1
)

REM 9) Overeni vystupu
if not exist "%SETUP_EXE%" (
    echo.
    echo CHYBA: Instalator nebyl vytvoren.
    echo Ocekavana cesta:
    echo %SETUP_EXE%
    echo.
    echo Zkontrolujte, jestli IExpress nevypsal chybu.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  HOTOVO
echo ============================================================
echo Instalator vytvoren zde:
echo %SETUP_EXE%
echo.
dir "%SETUP_EXE%"
echo.
pause
exit /b 0
