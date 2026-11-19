@echo off
rem Daily production checkpoint runner (scheduled task OLBR_PostCutoverCheck, 07:30).
rem No pause on purpose: a scheduled run must never wait for input.
rem Output goes to TMP\post-cutover-check\last-run.log (same folder as the JSON report).
chcp 65001 >nul
setlocal
set "ROOT=%~dp0..\.."
pushd "%ROOT%" || exit /b 3
if not exist "TMP\post-cutover-check" mkdir "TMP\post-cutover-check"
powershell -NoProfile -ExecutionPolicy Bypass -File "ops\deploy\post-cutover-check.ps1" -Render > "TMP\post-cutover-check\last-run.log" 2>&1
set RC=%ERRORLEVEL%
popd
exit /b %RC%
