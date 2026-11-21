@echo off
REM ============================================================
REM  OLBR production stack - start (my_app + portal + nginx)
REM  Delegates to portal-ctl.ps1 -Action start, which clears the
REM  "stopped by hand" flags and triggers the three self-healing
REM  scheduled tasks - the same path the 5-minute watchdog uses, so
REM  "started by hand" and "started after reboot" cannot drift apart.
REM  ASCII-only + CRLF on purpose; non-ASCII breaks cmd parsing.
REM ============================================================
setlocal
cd /d "%~dp0..\.."
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\deploy\portal-ctl.ps1" -Action start

echo.
echo ---- done. Press any key to close. ----
pause >nul
endlocal
