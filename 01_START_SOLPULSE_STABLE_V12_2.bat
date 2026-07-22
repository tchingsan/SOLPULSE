@echo off
setlocal
title SOLPULSE STABLE PAPER PILOT V12.2 - PORT 8527
cd /d "%~dp0"

echo.
echo ============================================================
echo          SOLPULSE STABLE PAPER PILOT V12.2
echo                     PORT 8527
echo ============================================================
echo.
echo PAPER UNIQUEMENT - aucune transaction reelle.
echo Paper Pilot : 0,01 SOL apres 25 secondes.
echo Acquisition complete : 0,05 SOL apres Safety complet.
echo Mayhem : toujours interdit.
echo.
echo IMPORTANT : le test synthetique ne bloque plus le demarrage.
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/6] Creation de l'environnement Python...
    python -m venv .venv
    if errorlevel 1 (
        echo ERREUR : Python introuvable.
        echo Installe Python et coche Add Python to PATH.
        pause
        exit /b 1
    )
) else (
    echo [1/6] Environnement Python present.
)

call ".venv\Scripts\activate.bat"

echo [2/6] Verification des dependances...
python bootstrap.py
if errorlevel 1 (
    echo ERREUR : dependances non installees.
    pause
    exit /b 1
)

if not exist "data\trading.db" (
    echo [3/6] Creation de la base paper...
    python reset_simulation.py
    if errorlevel 1 (
        echo ERREUR : creation de la base impossible.
        pause
        exit /b 1
    )
) else (
    echo [3/6] Base paper presente.
)

echo [4/6] Migration et verification SQLite...
python migrate_database.py
if errorlevel 1 (
    echo ERREUR : migration SQLite impossible.
    pause
    exit /b 1
)

echo [5/6] Diagnostic rapide non bloquant...
python self_test.py --startup
if errorlevel 1 (
    echo AVERTISSEMENT : diagnostic incomplet.
    echo SOLPULSE va quand meme demarrer.
    echo Consulte logs\startup_self_test.json.
)

for %%F in (
    prebond_bot.lock
    new_coin_radar.lock
    hybrid_market_scanner.lock
    safety_engine.lock
    qualification_pipeline.lock
    event_recorder.lock
    supervisor.lock
) do if exist "data\%%F" del /q "data\%%F" >nul 2>&1

if not exist "logs" mkdir "logs"
if not exist "backups" mkdir "backups"
if not exist "data\diagnostics" mkdir "data\diagnostics"

echo [6/6] Demarrage des moteurs et du dashboard...
start "SOLPULSE V12.2 - MOTEURS" /min cmd /k ""%CD%\.venv\Scripts\python.exe" -u "%CD%\supervisor.py""

timeout /t 6 /nobreak >nul
start "" "http://localhost:8527/?version=STABLE-PAPER-PILOT-V12-2"

python -m streamlit run app.py ^
  --server.port 8527 ^
  --server.address localhost ^
  --server.headless true ^
  --browser.gatherUsageStats false

echo.
echo Le dashboard Streamlit s'est arrete.
echo Consulte les fichiers dans logs si une erreur est affichee.
pause
endlocal
