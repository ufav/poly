@echo off
rem Tennis -> data\tennis.sqlite. Every step resumes on re-run; run steps in separate consoles in parallel.
rem   scripts\collect_tennis.bat             Polymarket ATP + WTA + ITF: events since 10.2025, minute prices >= $1000
rem   scripts\collect_tennis.bat trades      Polymarket taker trades of match markets >= $50k (long)
rem   scripts\collect_tennis.bat olimpbet    Olimpbet results by day: tournament city / country / surface (~2.5 h)
rem   scripts\collect_tennis.bat tennisdata  tennis-data.co.uk ATP / WTA 2025-2026: round, ranks, bookmaker odds (1 min)
rem   scripts\collect_tennis.bat link        Polymarket ↔ Olimpbet ↔ tennis-data + features (jetlag, home, fatigue)
cd /d "%~dp0.."
set PY=.venv\Scripts\python.exe
if "%1"=="trades" (
    %PY% -u scripts\collect_trades.py --sport tennis --min-volume 50000
    exit /b
)
if "%1"=="olimpbet" (
    %PY% -u scripts\collect_olimpbet.py --since 2025-10-01
    exit /b
)
if "%1"=="tennisdata" (
    %PY% -u scripts\collect_tennisdata.py --years 2025 2026
    exit /b
)
if "%1"=="link" (
    %PY% -u scripts\link_tennis.py
    exit /b
)
%PY% -u scripts\collect_polymarket.py --sport tennis --since 2025-10-01 --history-min-volume 1000
