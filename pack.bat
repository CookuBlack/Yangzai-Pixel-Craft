@echo off
chcp 936 >nul
title YangZai Pack (Full/torch)
cd /d "%~dp0"

echo ==========================================================
echo   One-click pack: Full version (with torch / AI watermark)
echo   Output: dist\yangzai
echo ==========================================================
echo.

set "PY=D:\App\Miniconda\envs\exp311\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -V >nul 2>nul
if errorlevel 1 goto :nopy
echo [1/4] Python:
"%PY%" -V
echo.

echo [2/4] Check PyInstaller ...
"%PY%" -m pip show pyinstaller >nul 2>nul
if errorlevel 1 goto :noinst
echo  - Installed.
goto :build
:noinst
echo  - Not found. Installing, needs internet ...
"%PY%" -m pip install pyinstaller
if errorlevel 1 goto :piperr
:build
echo.

echo [3/4] Building (onedir + torch, may take a while) ...
echo      No output here for a long time is NORMAL, keep window open.
"%PY%" -m PyInstaller watermark_app.spec --noconfirm --clean
if errorlevel 1 goto :builderr
echo.
echo  - Build done.
echo.

echo [4/4] Assemble: dist\yangzai ...
set "APP=dist\yangzai"
if not exist "%APP%" mkdir "%APP%"

if exist "dist\sheep_app\sheep_app.exe" move /Y "dist\sheep_app\sheep_app.exe" "%APP%\Yangzai Pixel Craft.exe" >nul 2>nul
if exist "dist\sheep_app\_internal" robocopy "dist\sheep_app\_internal" "%APP%\_internal" /E /MOVE /NFL /NDL /NJH /NJS >nul

REM 注意：cudnn_engines_precompiled64_9.dll 是 torch 2.5.1+cu121 / cudnn 9 的
REM 必需运行库（GPU conv 依赖），删除会导致 AI 去水印报
REM CUDNN_STATUS_NOT_INITIALIZED。请勿剔除！
if exist "dist\sheep_app" rmdir /S /Q "dist\sheep_app"
if exist "build" rmdir /S /Q "build"

if exist backend robocopy backend "%APP%\backend" /E /NFL /NDL /NJH /NJS /XD __pycache__ >nul 2>nul
for %%D in (frontend model lib asset) do if exist "%%D" robocopy "%%D" "%APP%\%%D" /E /NFL /NDL /NJH /NJS /XD __pycache__ .git .trae >nul 2>nul
if exist third_party robocopy third_party "%APP%\third_party" /E /NFL /NDL /NJH /NJS /XD __pycache__ >nul 2>nul
if exist data robocopy "data" "%APP%\data" /E /NFL /NDL /NJH /NJS /XD __pycache__ .git .trae jobs uploads staging output preview wmout >nul 2>nul
if exist dlss robocopy dlss "%APP%\dlss" /E /NFL /NDL /NJH /NJS /XD logs outputs jobs __pycache__ >nul 2>nul

echo.
echo ==========================================================
echo  DONE! Folder: %APP%
echo  Run: %APP%\Yangzai Pixel Craft.exe
echo ==========================================================
goto :end
:nopy
echo [ERR] Python not found: %PY%
goto :end
:piperr
echo [ERR] pip install pyinstaller failed.
goto :end
:builderr
echo.
echo [ERR] Build failed! Please screenshot the error.
goto :end
:end
pause