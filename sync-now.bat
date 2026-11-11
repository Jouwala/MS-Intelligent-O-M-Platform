@echo off
setlocal EnableExtensions
title Sync source from main repo into portable copy
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo  ============================================================
echo   Portable copy - transactional source sync (source READ-ONLY)
echo   Backups stay in this folder's TMP\
echo  ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\sync_from_main.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
echo  ------------------------------------------------------------
echo   Finished. Exit code: %RC%
echo   Preview only  -^>  sync-now.bat -DryRun
echo   Accept source -^>  sync-now.bat -AcceptSource  (backs up local edits)
echo  ------------------------------------------------------------
echo.

echo %* | findstr /I /C:"-NoPause" >nul 2>&1
if errorlevel 1 pause
endlocal & exit /b %RC%
