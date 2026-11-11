@echo off
setlocal
cd /d "%~dp0"

rem Prefer the packaged Core executable produced by GisCodeCore.spec.
if exist "%~dp0dist\GisCode.exe" (
    start "" "%~dp0dist\GisCode.exe" %*
    exit /b 0
)

rem Also work when this launcher is copied next to the EXE.
if exist "%~dp0GisCode.exe" (
    start "" "%~dp0GisCode.exe" %*
    exit /b 0
)

rem Source-launcher fallback for development.
if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" (
    start "GisCode Core" "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" -m giscode.main
    exit /b 0
)

if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "GisCode Core" "%~dp0.venv\Scripts\pythonw.exe" -m giscode.main
    exit /b 0
)

echo Could not find GisCode.exe or a Python source runtime.
exit /b 1
