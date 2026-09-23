@echo off
REM Windows launcher, twin of the POSIX `garmin` script. Put this folder on your
REM PATH (or make a shortcut) and `garmin doctor` works from anywhere.
REM %~dp0 is this file's own directory, with a trailing backslash.
setlocal
set "HERE=%~dp0"
if exist "%HERE%.venv\Scripts\python.exe" (
  "%HERE%.venv\Scripts\python.exe" "%HERE%garmin.py" %*
) else (
  echo No virtualenv found in "%HERE%.venv". Run setup.ps1 first. 1>&2
  exit /b 1
)
exit /b %ERRORLEVEL%
