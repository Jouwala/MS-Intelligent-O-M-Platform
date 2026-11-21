@echo off
REM ============================================================
REM  OLBR production stack - open the portal home page
REM  Production HTTPS is fixed at 443 by ops\deploy\portal-ctl.ps1.
REM  Do NOT read the port from .env: this tree's .env still holds the
REM  isolated-copy value (8443) and would open a dead URL (the old
REM  version read ops\.env, which does not even exist).
REM  ASCII-only + CRLF on purpose; non-ASCII breaks cmd parsing.
REM ============================================================
setlocal
netstat -ano | findstr /r /c:":443 .*LISTENING" >nul
if errorlevel 1 (
  echo WARNING: nothing is listening on 443 - the stack looks down.
  echo          Run 0-status.bat to see it, or 1-start-all.bat to start it.
  echo.
)
echo Opening https://localhost/ ...
start "" "https://localhost/"

echo.
echo ---- done. Press any key to close. ----
pause >nul
endlocal
