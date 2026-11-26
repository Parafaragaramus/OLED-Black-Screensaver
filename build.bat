@echo off
REM ---------------------------------------------------------------
REM Builds black_screensaver.py into a standalone .exe.
REM Run this once (on a machine that has Python installed) to
REM produce BlackScreensaver.exe. After that, the .exe can be
REM copied to any Windows PC and run with no Python required.
REM
REM Usage: put this file in the same folder as black_screensaver.py
REM and double-click it (or run it from a terminal).
REM ---------------------------------------------------------------

REM Try a few ways to find Python, since "py" and "python" availability
REM varies by machine/install method. Report clearly what was tried.
set PYCMD=

py --version >nul 2>nul
if not errorlevel 1 (
    set PYCMD=py
    goto :found
)

python --version >nul 2>nul
if not errorlevel 1 (
    set PYCMD=python
    goto :found
)

python3 --version >nul 2>nul
if not errorlevel 1 (
    set PYCMD=python3
    goto :found
)

REM Last resort: check the common per-user install locations directly,
REM in case PATH isn't set up but Python IS installed.
for %%V in (Python314 Python313 Python312 Python311 Python310) do (
    if exist "%LOCALAPPDATA%\Programs\Python\%%V\python.exe" (
        set PYCMD="%LOCALAPPDATA%\Programs\Python\%%V\python.exe"
        goto :found
    )
)

echo.
echo Could not find a working Python command. Tried: py, python, python3,
echo and common install folders under %LOCALAPPDATA%\Programs\Python\
goto :fail

:found

echo Using command: %PYCMD%
%PYCMD% --version
if errorlevel 1 (
    echo.
    echo ================================================================
    echo Could not run "%PYCMD%". See the troubleshooting notes below.
    echo ================================================================
    goto :fail
)

echo.
echo Installing required packages...
%PYCMD% -m pip install --upgrade pip
%PYCMD% -m pip install pyinstaller pywin32 pystray pillow comtypes mss

echo.
echo Building BlackScreensaver.exe ...
%PYCMD% -m PyInstaller --onefile --noconsole --name BlackScreensaver ^
    --hidden-import "pystray._win32" ^
    --hidden-import "win32timezone" ^
    --hidden-import "PIL._tkinter_finder" ^
    --hidden-import "pythoncom" ^
    --hidden-import "pywintypes" ^
    --hidden-import "win32com" ^
    --hidden-import "win32com.client" ^
    --hidden-import "comtypes.stream" ^
    --hidden-import "mss.windows" ^
    --collect-submodules "comtypes" ^
    black_screensaver.py

echo.
echo ================================================================
echo Build complete (if no errors appeared above).
echo Your standalone exe is at:  dist\BlackScreensaver.exe
echo You can copy just that one file anywhere -- no Python needed.
echo.
echo If the exe doesn't seem to start, check for a file named
echo black_screensaver_error.log next to it -- the app now logs
echo startup errors there and also shows a popup if something fails.
echo ================================================================
pause
goto :eof

:fail
echo If "py" was not found either:
echo   1. Open Settings ^> Apps ^> Advanced app settings ^>
echo      App execution aliases, and turn OFF the "python.exe" and
echo      "python3.exe" entries (these Store stubs can shadow real
echo      Python installs even when py.exe exists).
echo   2. Or reinstall Python from python.org and make sure
echo      "Add python.exe to PATH" is checked during install.
echo   3. Then close and reopen this terminal/Explorer window and
echo      try build.bat again.
pause
