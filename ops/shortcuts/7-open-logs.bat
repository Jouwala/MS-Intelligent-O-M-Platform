@echo off
REM ============================================================
REM  OLBR production stack - open the runtime log folders
REM  Root is TWO levels up from this file (ops\shortcuts\ -> tree root).
REM  The old version used %~dp0.. (i.e. ops\) and therefore looked for
REM  ops\TMP and ops\nginx-1.28.0\logs, neither of which exists.
REM  ASCII-only + CRLF on purpose; non-ASCII breaks cmd parsing.
REM ============================================================
setlocal
set "ROOT=%~dp0..\.."
if exist "%ROOT%\TMP" (
  start "" explorer.exe "%ROOT%\TMP"
) else (
  echo TMP folder missing: %ROOT%\TMP
)
if exist "%ROOT%\nginx-1.28.0\logs" (
  start "" explorer.exe "%ROOT%\nginx-1.28.0\logs"
) else (
  echo nginx log folder missing: %ROOT%\nginx-1.28.0\logs
)

echo.
echo ---- done. Press any key to close. ----
pause >nul
endlocal
