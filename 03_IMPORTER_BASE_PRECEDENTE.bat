@echo off
title SOLPULSE V12.2 - IMPORTER UNE BASE PRECEDENTE
cd /d "%~dp0"

echo.
echo Ferme toutes les fenetres SOLPULSE avant l'import.
echo Une fenetre te demandera de selectionner le fichier trading.db de ta version precedente.
echo Pour corriger V12 sans perdre l historique, choisis :
echo SOLPULSE-STABLE-PAPER-PILOT-V12\data\trading.db
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" import_v9_database.py
) else (
    python import_v9_database.py
)

pause
