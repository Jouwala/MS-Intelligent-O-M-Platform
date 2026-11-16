@echo off
REM OLBR portal - stop everything THIS directory runs.
REM
REM Mode is chosen the same way start.bat chooses it (see that file):
REM   * ISOLATED_COPY.flag present -> scripts\stop_all.ps1 (pid-file based; it
REM     only ever stops processes this copy started)
REM   * flag absent (promoted)     -> my_portal_ctl.ps1 9-stop-all (nginx ->
REM     portal -> my_app -> bot, the production orchestrator)
REM
REM stop_all.ps1 on a promoted copy is a silent no-op: it only knows the
REM TMP\portable-*.pid files, and production processes are started by
REM start_hidden.ps1 / watchdogs, which never write those. Saying "stopped"
REM while everything keeps serving is the worst possible outcome here.
REM
REM Keep this file ASCII-only and CRLF; non-ASCII breaks cmd parsing.
setlocal
cd /d "%~dp0"

if exist "%~dp0ISOLATED_COPY.flag" goto isolated
if exist "%~dp0my_portal_ctl.ps1" goto promoted

echo [FAIL] Neither ISOLATED_COPY.flag nor my_portal_ctl.ps1 exists in %~dp0
echo        Refusing to guess which mode this directory is in.
exit /b 2

:isolated
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_all.ps1" %*
exit /b %errorlevel%

:promoted
if not "%~1"=="" (
    echo [FAIL] Promoted copy: switch arguments are isolated-only ^(-DryRun^).
    echo        Refusing to stop with: %*
    exit /b 2
)
echo [*] Promoted copy (no ISOLATED_COPY.flag): my_portal_ctl.ps1 9-stop-all
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0my_portal_ctl.ps1" 9-stop-all
exit /b %errorlevel%
