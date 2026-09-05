@echo off
setlocal enabledelayedexpansion
title Practice Helper installer

REM Practice Helper - Windows installer.
REM
REM Double-click this file. It fetches the app if needed, builds an isolated
REM Python environment, and puts shortcuts on your Desktop and Start Menu.
REM
REM Deliberately a bootstrap installer rather than a frozen .exe: the stem
REM separator pulls in PyTorch, which is several gigabytes and notoriously
REM fragile to freeze. Building the environment here is smaller to download
REM and far more likely to work.
REM
REM Structured with subroutines rather than parenthesised blocks on purpose:
REM inside "if (...)" batch treats a bare ")" as the end of the block, which
REM silently mangles any echoed text containing one.

set "REPO=https://github.com/caleblanc/practice-helper"
set "APPNAME=Practice Helper"

cd /d "%~dp0"
echo.
echo  Practice Helper installer
echo  =========================
echo.

call :find_python
if errorlevel 1 goto :no_python

if not exist "app.py" call :fetch_source
if errorlevel 1 goto :download_failed
set "APPSRC=%CD%"

call :make_env
if errorlevel 1 goto :env_failed

call :install_deps
if errorlevel 1 goto :deps_failed

call :check_ffmpeg
call :make_shortcuts

echo.
echo  Done.
echo  Launch "%APPNAME%" from your Desktop or Start Menu.
echo  On first launch it will offer to set up a streaming service.
echo.
set /p "OPENIT=Open it now? [Y/n] "
if /i not "!OPENIT!"=="n" start "" "!PYW!" "%APPSRC%\app.py"
echo.
pause
exit /b 0


REM ============================ subroutines ==================================

:find_python
set "PY="
call :try_python "py -3"
call :try_python "python"
call :try_python "python3"
if not defined PY exit /b 1
for /f "delims=" %%V in ('%PY% --version 2^>^&1') do echo  [OK] Using %%V
exit /b 0

:try_python
if defined PY exit /b 0
%~1 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0

:fetch_source
echo.
echo  Downloading Practice Helper...
set "SRC=%LOCALAPPDATA%\Practice Helper"
where git >nul 2>&1
if not errorlevel 1 goto :fetch_git
powershell -NoProfile -ExecutionPolicy Bypass -Command "iwr '%REPO%/archive/refs/heads/main.zip' -OutFile $env:TEMP\ph.zip; Expand-Archive $env:TEMP\ph.zip $env:TEMP\phstage -Force; $i=(gci $env:TEMP\phstage -Directory)[0].FullName; if(Test-Path '%SRC%'){ri '%SRC%' -Recurse -Force}; mi $i '%SRC%'"
if errorlevel 1 exit /b 1
goto :fetch_done

:fetch_git
if exist "%SRC%" rmdir /s /q "%SRC%"
git clone --depth 1 -q "%REPO%" "%SRC%"
if errorlevel 1 exit /b 1

:fetch_done
cd /d "%SRC%"
echo  [OK] Downloaded to %SRC%
exit /b 0

:make_env
echo.
echo  Setting up Python environment
echo  (this installs PyTorch and can take several minutes the first time)
if exist ".venv\Scripts\python.exe" goto :env_ok
%PY% -m venv .venv
if errorlevel 1 exit /b 1
:env_ok
echo  [OK] Environment ready
exit /b 0

:install_deps
echo.
echo  Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
echo  [OK] Dependencies installed
exit /b 0

:check_ffmpeg
call :make_shortcuts

echo.
echo  Done.
echo  Launch "%APPNAME%" from your Desktop or Start Menu.
echo  On first launch it will offer to set up a streaming service.
echo.
set /p "OPENIT=Open it now? [Y/n] "
if /i not "!OPENIT!"=="n" start "" "!PYW!" "%APPSRC%\app.py"
echo.
pause
exit /b 0


REM ============================ subroutines ==================================

:find_python
set "PY="
call :try_python "py -3"
call :try_python "python"
call :try_python "python3"
if not defined PY exit /b 1
for /f "delims=" %%V in ('%PY% --version 2^>^&1') do echo  [OK] Using %%V
exit /b 0

:try_python
if defined PY exit /b 0
%~1 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0

:fetch_source
echo.
echo  Downloading Practice Helper...
set "SRC=%LOCALAPPDATA%\Practice Helper"
where git >nul 2>&1
if not errorlevel 1 goto :fetch_git
powershell -NoProfile -ExecutionPolicy Bypass -Command "iwr '%REPO%/archive/refs/heads/main.zip' -OutFile $env:TEMP\ph.zip; Expand-Archive $env:TEMP\ph.zip $env:TEMP\phstage -Force; $i=(gci $env:TEMP\phstage -Directory)[0].FullName; if(Test-Path '%SRC%'){ri '%SRC%' -Recurse -Force}; mi $i '%SRC%'"
if errorlevel 1 exit /b 1
goto :fetch_done

:fetch_git
if exist "%SRC%" rmdir /s /q "%SRC%"
git clone --depth 1 -q "%REPO%" "%SRC%"
if errorlevel 1 exit /b 1

:fetch_done
cd /d "%SRC%"
echo  [OK] Downloaded to %SRC%
exit /b 0

:make_env
echo.
echo  Setting up Python environment
echo  (this installs PyTorch and can take several minutes the first time)
if exist ".venv\Scripts\python.exe" goto :env_ok
%PY% -m venv .venv
if errorlevel 1 exit /b 1
:env_ok
echo  [OK] Environment ready
exit /b 0

:install_deps
echo.
echo  Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
echo  [OK] Dependencies installed
exit /b 0

:check_ffmpeg
REM ffmpeg is genuinely required on Windows - there is no afconvert here.
where ffmpeg >nul 2>&1
if not errorlevel 1 exit /b 0
echo.
echo  [!] ffmpeg was NOT found, and it is REQUIRED on Windows for audio.
echo      Install it with:  winget install Gyan.FFmpeg
echo      then reopen your terminal. Stem extraction will not work without it.
exit /b 0

:make_shortcuts
echo.
echo  Creating shortcuts...
set "PYW=%APPSRC%\.venv\Scripts\pythonw.exe"
if not exist "!PYW!" set "PYW=%APPSRC%\.venv\Scripts\python.exe"
powershell -NoProfile -ExecutionPolicy Bypass -File "%APPSRC%\scripts\win_shortcuts.ps1" -Target "!PYW!" -AppDir "%APPSRC%" -Name "%APPNAME%"
if errorlevel 1 echo  [!] Could not create shortcuts - run app.py from %APPSRC%
if not errorlevel 1 echo  [OK] Shortcuts created
exit /b 0


REM ============================== failures ===================================

:no_python
echo  [X] Python 3.11 or newer is required but was not found.
echo.
echo      Install it from https://www.python.org/downloads/
echo      IMPORTANT: tick "Add python.exe to PATH" in the installer.
echo      Then run this file again.
echo.
pause
exit /b 1

:download_failed
echo  [X] Download failed. Check your internet connection.
pause
exit /b 1

:env_failed
echo  [X] Could not create the virtual environment.
pause
exit /b 1

:deps_failed
echo.
echo  [X] Dependency installation failed. The messages above say why.
pause
exit /b 1
