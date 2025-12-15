@echo off
setlocal
cd /d "%~dp0"

REM Prefer GUI launcher (pythonw/pyw)
where pyw >nul 2>nul
if %errorlevel%==0 (
  pyw -3 "%~dp0JellyfinOrganizer.pyw"
  goto :eof
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
  pythonw "%~dp0JellyfinOrganizer.pyw"
  goto :eof
)

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0JellyfinOrganizer.pyw"
  goto :eof
)

where python >nul 2>nul
if %errorlevel%==0 (
  python "%~dp0JellyfinOrganizer.pyw"
  goto :eof
)

echo.
echo Python 3 wurde nicht gefunden.
echo Bitte installiere Python 3.11 oder neuer (inkl. Tkinter) und starte dann erneut.
echo.
pause
