@echo off
rem Volleyball -> data\volleyball.sqlite. Re-runs resume.
rem Prefer Polymarket first, then olimpbet. Same db; parallel OK but can briefly lock.
rem   scripts\collect_volleyball.bat             Polymarket: all vb series since 2025-01, minute prices
rem   scripts\collect_volleyball.bat trades      Taker trades moneyline min volume 1000
rem   scripts\collect_volleyball.bat olimpbet    Olimpbet sport-id 104 sets since 2025-10-01, hours
rem   scripts\collect_volleyball.bat calib       Pre-match calibration
rem
rem Progress:  python -u scripts\progress_volleyball.py
cd /d "%~dp0.."
set PY=.venv\Scripts\python.exe
if "%1"=="trades" goto trades
if "%1"=="olimpbet" goto olimpbet
if "%1"=="calib" goto calib
%PY% -u scripts\collect_polymarket.py --sport volleyball --since 2025-01-01 --history-min-volume 0
exit /b

:trades
%PY% -u scripts\collect_trades.py --sport volleyball --min-volume 1000
exit /b

:olimpbet
%PY% -u scripts\collect_olimpbet.py --sport volleyball --since 2025-10-01
exit /b

:calib
%PY% -u analysis\volleyball_calib.py --min-volume 0 --min-n 10
exit /b
