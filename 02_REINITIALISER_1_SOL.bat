@echo off
title SOLPULSE V12.2 - RESET COMPLET
cd /d "%~dp0"
echo.
echo Ferme d'abord le superviseur et le dashboard SOLPULSE.
echo Cette action efface :
echo - les trades paper
echo - l'historique du radar
echo - les evaluations Safety
echo - les candidats de strategie
echo - les donnees Replay et les backtests
echo.
echo La watchlist de cinq contrats reste intacte.
echo.
choice /C ON /M "Continuer ? O=Oui, N=Non"
if errorlevel 2 exit /b

if exist "data\prebond_bot.lock" del /q "data\prebond_bot.lock"
if exist "data\new_coin_radar.lock" del /q "data\new_coin_radar.lock"
if exist "data\hybrid_market_scanner.lock" del /q "data\hybrid_market_scanner.lock"
if exist "data\safety_engine.lock" del /q "data\safety_engine.lock"
if exist "data\qualification_pipeline.lock" del /q "data\qualification_pipeline.lock"
if exist "data\event_recorder.lock" del /q "data\event_recorder.lock"
if exist "data\supervisor.lock" del /q "data\supervisor.lock"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" reset_simulation.py
) else (
    python reset_simulation.py
)
pause
