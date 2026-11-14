@echo off
rem ---------------------------------------------------------------------------
rem Builds dist\PhoneDesk.exe (single file, no Python needed on the target PC).
rem Run from anywhere; requires the .venv created during setup.
rem ---------------------------------------------------------------------------
setlocal

rem %~dp0 = folder this .bat lives in. cd there so relative paths work no
rem matter where the script was started from. /d also switches drive letter.
cd /d "%~dp0"

rem Use the venv's python explicitly instead of relying on "activate":
rem PyInstaller bundles whatever is installed in the interpreter that runs it,
rem so building from the wrong Python silently produces an exe missing packages.
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo ERROR: .venv not found. Create it first:
    echo   py -3 -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r server\requirements.txt
    exit /b 1
)

"%PY%" -m pip install --quiet pyinstaller
if errorlevel 1 exit /b 1

rem --onefile              one exe; unpacks to a temp folder on each start
rem --collect-submodules   dxcam is imported lazily inside a function and loads
rem                        its own parts dynamically; make sure all of it ships
rem --hidden-import        mss picks its Windows module at runtime by OS name
rem No --noconsole yet: until the tray icon exists (milestone 8) the console
rem window is the only way to see the address, the log and to stop the server.
"%PY%" -m PyInstaller --noconfirm --clean --onefile --name PhoneDesk ^
    --collect-submodules dxcam ^
    --hidden-import mss.windows ^
    run_server.py
if errorlevel 1 (
    echo BUILD FAILED
    exit /b 1
)

echo.
echo Built: %~dp0dist\PhoneDesk.exe
endlocal
