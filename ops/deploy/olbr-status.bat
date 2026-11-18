@echo off
chcp 65001 >nul 2>&1
title OLBR - status (D:\02_olbr_portal)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0portal-ctl.ps1" -Action status
echo.
pause
