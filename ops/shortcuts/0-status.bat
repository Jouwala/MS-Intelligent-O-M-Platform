@echo off
REM ============================================================
REM  OLBR production stack - status (READ ONLY)
REM  Ports / HTTP health / self-healing task state come from the one
REM  production control script: ops\deploy\portal-ctl.ps1.
REM  Nothing here starts or stops a service.
REM  ASCII-only + CRLF on purpose; non-ASCII breaks cmd parsing.
REM ============================================================
setlocal
cd /d "%~dp0..\.."
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\deploy\portal-ctl.ps1" -Action status

echo.
echo ---- done. Press any key to close. ----
pause >nul
endlocal
