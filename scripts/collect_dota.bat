@echo off
rem Dota 2 full collection: cybersport -> rosters -> OpenDota -> map links. Re-running resumes.
rem Prices in parallel (another console): scripts\collect_dota.bat prices
cd /d "%~dp0.."
set PY=.venv\Scripts\python.exe
if "%1"=="prices" (
    %PY% -u scripts\collect_polymarket.py --sport dota2 --histories-only
    exit /b
)
%PY% -u scripts\collect_cybersport.py --sport dota2 --since 2025-06-01
%PY% -u scripts\collect_cs_teams.py --sport dota2
%PY% -u scripts\collect_opendota.py
%PY% -u scripts\link_dota.py
