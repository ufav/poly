@echo off
rem Fat Polymarket sports -> data\<sport>.sqlite. Re-runs resume. Do NOT run all at once in one console
rem unless you want a long night; better one sport per console.
rem
rem   scripts\collect_fat.bat nfl
rem   scripts\collect_fat.bat nba
rem   scripts\collect_fat.bat nhl
rem   scripts\collect_fat.bat lol
rem   scripts\collect_fat.bat ufc
rem   scripts\collect_fat.bat fifa
rem   scripts\collect_fat.bat soccer
rem   scripts\collect_fat.bat all
rem
rem Optional 2nd arg: trades | histories
rem   scripts\collect_fat.bat nba trades
rem   scripts\collect_fat.bat soccer histories
rem
rem Progress:  python -u scripts\progress_pm.py --sport nba
rem
rem Histories only for moneyline vol >= 1000 USD. Since 2025-08-01.
cd /d "%~dp0.."
set PY=.venv\Scripts\python.exe
set SPORT=%1
set MODE=%2
if "%SPORT%"=="" (
    echo Usage: collect_fat.bat nfl^|nba^|nhl^|lol^|ufc^|fifa^|soccer^|all [trades^|histories]
    exit /b 1
)
if /I "%SPORT%"=="all" goto all
if /I "%MODE%"=="trades" goto trades
if /I "%MODE%"=="histories" goto histories
%PY% -u scripts\collect_polymarket.py --sport %SPORT% --since 2025-08-01 --history-min-volume 1000
exit /b

:trades
%PY% -u scripts\collect_trades.py --sport %SPORT% --min-volume 50000
exit /b

:histories
%PY% -u scripts\collect_polymarket.py --sport %SPORT% --histories-only --history-min-volume 1000
exit /b

:all
for %%S in (nfl nba nhl lol ufc fifa soccer) do (
    echo ===== %%S =====
    %PY% -u scripts\collect_polymarket.py --sport %%S --since 2025-08-01 --history-min-volume 1000
)
exit /b
