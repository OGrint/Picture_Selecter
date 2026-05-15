@echo off
setlocal EnableDelayedExpansion
REM ============================================================
REM  Photo Reviewer — Windows Desktop App Builder
REM  Place in same folder as photo_reviewer.py then double-click
REM ============================================================

echo.
echo  ===================================
echo   Photo Reviewer — Build Script
echo  ===================================
echo.

if not exist "photo_reviewer.py" (
    echo  ERROR: photo_reviewer.py not found in this folder.
    pause & exit /b 1
)

if not exist "photo_reviewer.spec" (
    echo  ERROR: photo_reviewer.spec not found in this folder.
    pause & exit /b 1
)

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: Python not found. Run from Anaconda Prompt.
    pause & exit /b 1
)

echo  Python: & python --version
echo.
echo  [1/4] Installing packages...
pip install pyinstaller customtkinter pillow rawpy imagehash send2trash numpy --quiet
echo  Done.
echo.

if not exist "photo_reviewer_icon.ico" (
    echo  NOTE: photo_reviewer_icon.ico not found - app will use default icon.
    echo.
)

if not exist "photo_reviewer_countries.geojson" (
    echo  NOTE: photo_reviewer_countries.geojson not found - location map disabled.
    echo.
)

echo  [2/4] Building application (3-5 minutes)...
echo.
pyinstaller photo_reviewer.spec --clean --noconfirm

if %errorlevel% neq 0 (
    echo.
    echo  BUILD FAILED. Common fixes:
    echo    1. Run from Anaconda Prompt not regular cmd
    echo    2. pip install pyinstaller==6.3.0
    echo    3. Add missing module to hiddenimports in photo_reviewer.spec
    pause & exit /b 1
)

echo.
echo  [3/4] Copying support files...

if exist "photo_reviewer_countries.geojson" (
    copy /Y "photo_reviewer_countries.geojson" "dist\PhotoReviewer\" >nul
    echo  Copied: photo_reviewer_countries.geojson
)

if exist "photo_reviewer_icon.ico" (
    copy /Y "photo_reviewer_icon.ico" "dist\PhotoReviewer\" >nul
    echo  Copied: photo_reviewer_icon.ico
)

echo.
echo  [4/4] Done!
echo.
echo  ===================================
echo   BUILD SUCCESSFUL
echo  ===================================
echo.
echo  Your app:  dist\PhotoReviewer\PhotoReviewer.exe
echo  To share:  Zip the entire dist\PhotoReviewer\ folder
echo.
pause
