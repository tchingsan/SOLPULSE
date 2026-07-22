@echo off
setlocal
title SOLPULSE V12.2 - TEST ACHAT PAPER ISOLE
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Lance d'abord 01_START_SOLPULSE_STABLE_V12_2.bat.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
echo.
echo Test isole : la vraie base data\trading.db ne sera pas modifiee.
echo La base de test sera conservee dans data\diagnostics.
echo.
python self_test.py --full
echo.
echo Rapport : logs\startup_self_test.json
pause
endlocal
