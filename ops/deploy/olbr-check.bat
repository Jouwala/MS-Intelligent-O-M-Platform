@echo off
rem OLBR production checkpoint - manual runner (no schedule on purpose).
rem Runs ops\deploy\post-cutover-check.ps1 and keeps the window open.
rem Usage:  olbr-check.bat            (port/http/board/task/whatsapp checks)
rem         olbr-check.bat -Render    (also renders pages via Playwright)
chcp 65001 >nul
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0post-cutover-check.ps1" %*
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="0" (echo RESULT: PASS) else (echo RESULT: FAIL ^(exit %RC%^))
pause
exit /b %RC%
