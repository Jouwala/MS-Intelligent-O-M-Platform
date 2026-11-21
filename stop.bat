@echo off
REM ============================================================
REM  OLBR entry - STOP, routed by the runtime marker in this folder:
REM
REM    PRODUCTION.flag present -> ops\deploy\portal-ctl.ps1 -Action stop
REM                               (non-interactive by design: rollout and
REM                                rollback scripts call this path. The
REM                                interactive Y/N variant is
REM                                ops\deploy\olbr-stop.bat and
REM                                ops\shortcuts\2-stop-all.bat.)
REM    otherwise               -> ops\deploy\stop_all.ps1
REM                               (only stops pids recorded by start_all.ps1)
REM
REM  Two hard rules:
REM    1) stop_all.ps1 runs "nginx -s quit" but only knows TMP\portable-*.pid,
REM       which do not exist on a production tree - it would close the live
REM       entry point and leave the backends running.
REM    2) In production this entry takes NO arguments: "-DryRun" here would
REM       stop the stack for real. A stray argument is refused instead.
REM  Both scripts also carry their own guards; this file only routes.
REM
REM  ASCII-only + CRLF on purpose; non-ASCII breaks cmd parsing.
REM ============================================================
setlocal
cd /d "%~dp0"
set "RC=0"
if not exist "%~dp0PRODUCTION.flag" goto :isolated
if not "%~1"=="" goto :prod_args
echo [production] ops\deploy\portal-ctl.ps1 -Action stop
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\deploy\portal-ctl.ps1" -Action stop
set "RC=%ERRORLEVEL%"
goto :done

:prod_args
echo [production] this entry takes no arguments.
echo              Use ops\deploy\portal-ctl.ps1 -Action start^|stop^|status directly.
set "RC=2"
goto :done

:isolated
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\deploy\stop_all.ps1" %*
set "RC=%ERRORLEVEL%"

:done
endlocal & exit /b %RC%
