@echo off
echo ======================================================================
echo   Launching Launch Gate Full-Stack Environment...
echo ======================================================================

set "PATH=%LOCALAPPDATA%\Programs\nodejs;%PATH%"

if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)

python run_local.py
pause
