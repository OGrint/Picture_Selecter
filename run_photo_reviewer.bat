@echo off
rem Start Photo Reviewer with the Python environment named in local_setup.ini
rem (pythonw = ... under [paths]). local_setup.ini is not committed to git;
rem see docs\installation.md.
setlocal
cd /d "%~dp0"
set "PYW="
for /f "tokens=1,* delims==" %%a in ('findstr /r /b /c:"pythonw *=" local_setup.ini 2^>nul') do set "PYW=%%b"
for /f "tokens=*" %%c in ("%PYW%") do set "PYW=%%c"
if "%~1"=="--check" (echo %PYW% & exit /b 0)
if not defined PYW goto missing
if not exist "%PYW%" goto missing
start "" "%PYW%" "%~dp0photo_reviewer.py"
exit /b 0
:missing
echo Could not find the Python to run Photo Reviewer.
echo Put the full path to pythonw.exe of your Photo environment in local_setup.ini:
echo   [paths]
echo   pythonw = C:\...\miniconda3\envs\Photo\pythonw.exe
echo See docs\installation.md.
pause
exit /b 1
