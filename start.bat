@echo off
REM ============================================================
REM  OLBR entry - START, routed by the runtime marker in this folder:
REM
REM    PRODUCTION.flag present -> ops\deploy\portal-ctl.ps1 -Action start
REM                               (the live 80/443/7038/7090/9267/9268 stack,
REM                                same path as the 5-minute self-heal tasks)
REM    otherwise               -> ops\deploy\start_all.ps1
REM                               (the isolated launcher: rewrites backend\.env,
REM                                starts the portable PG on 55432 and binds the
REM                                isolated ports - correct only for a copy)
REM
REM  Two hard rules:
REM    1) start_all.ps1 is not "the start button", it is the ISOLATED start
REM       button. Running it on a production tree fights the live stack.
REM    2) In production this entry takes NO arguments. Swallowing something
REM       like -DryRun and starting for real would be the worst outcome, so
REM       a stray argument is refused instead. Use portal-ctl.ps1 directly.
REM  Both scripts also carry their own guards; this file only routes.
REM
REM  ASCII-only + CRLF on purpose; non-ASCII breaks cmd parsing.
REM ============================================================
setlocal
cd /d "%~dp0"
set "RC=0"
if not exist "%~dp0PRODUCTION.flag" goto :isolated
if not "%~1"=="" goto :prod_args
echo [production] ops\deploy\portal-ctl.ps1 -Action start
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\deploy\portal-ctl.ps1" -Action start
set "RC=%ERRORLEVEL%"
goto :done

:prod_args
echo [production] this entry takes no arguments.
echo              Use ops\deploy\portal-ctl.ps1 -Action start^|stop^|status directly.
set "RC=2"
goto :done

:isolated
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\deploy\start_all.ps1" %*
set "RC=%ERRORLEVEL%"

:done
endlocal & exit /b %RC%
