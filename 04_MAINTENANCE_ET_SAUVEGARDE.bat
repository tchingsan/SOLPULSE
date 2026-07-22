@echo off
title SOLPULSE V12.2 - MAINTENANCE
cd /d "%~dp0"

echo.
echo Ferme SOLPULSE avant une maintenance manuelle.
echo Verification de l'integrite, checkpoint WAL et sauvegarde...
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" db_maintenance.py
) else (
    python db_maintenance.py
)

pause
