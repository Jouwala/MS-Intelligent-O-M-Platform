@echo off
setlocal EnableExtensions
title Deploy: sync, rebuild frontend, verify
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo  ============================================================
echo   One step: sync source + rebuild frontend + verify
echo   Source repo is READ-ONLY. This does NOT restart services.
echo  ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "D:\01_RPA\scripts\deploy_to_copy.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
echo  ------------------------------------------------------------
echo   Finished. Exit code: %RC%
echo   Preview only    -^>  sync-now.bat -DryRun
echo   Source only     -^>  sync-now.bat -SkipBuild
echo   Accept source   -^>  sync-now.bat -AcceptSource
echo   Restart only if the script above says the backend is still old.
echo  ------------------------------------------------------------
echo.

echo %* | findstr /I /C:"-NoPause" >nul 2>&1
if errorlevel 1 pause
endlocal & exit /b %RC%
