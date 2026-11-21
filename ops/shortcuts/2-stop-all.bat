@echo off
REM ============================================================
REM  OLBR production stack - stop
REM  Forward ONLY: the Y/N confirmation lives in
REM  ops\deploy\olbr-stop.bat, so there is exactly one place that
REM  decides "stop everything?" (the old version called
REM  my_portal_ctl.ps1 9-stop-all and killed the stack with no prompt).
REM  ASCII-only + CRLF on purpose; non-ASCII breaks cmd parsing.
REM ============================================================
setlocal
cd /d "%~dp0..\.."
call "%~dp0..\deploy\olbr-stop.bat"
endlocal
