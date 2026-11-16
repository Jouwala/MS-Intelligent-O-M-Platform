@echo off
REM OLBR portal - start everything in THIS directory.
REM
REM Two modes, selected by the promotion switch (the copy-root flag):
REM   * ISOLATED_COPY.flag present -> portable/isolated copy: scripts\start_all.ps1
REM     (forces MY_APP_DISABLE_SCHEDULER=1, uses the bundled portable PG,
REM      and refuses any port that production uses)
REM   * flag absent (promoted)     -> production copy: my_portal_ctl.ps1 8-start-all
REM     (same orchestrator as the 8-start_all desktop shortcut:
REM      my_app -> portal -> whatsapp bot -> nginx; start_hidden.ps1 is
REM      idempotent, so running it with services up only fills in gaps)
REM
REM NEVER run start_all.ps1 on a promoted copy: it would disable the scheduler,
REM start a portable PG on the production port and reject 80/443 - i.e. exactly
REM the opposite of what this machine now needs. That is why the flag, not the
REM file name, decides the mode.
REM
REM -DryRun / -SkipNginxConf belong to the isolated starter and are therefore
REM only forwarded in isolated mode.
REM Keep this file ASCII-only and CRLF; non-ASCII breaks cmd parsing.
setlocal
cd /d "%~dp0"

if exist "%~dp0ISOLATED_COPY.flag" goto isolated
if exist "%~dp0my_portal_ctl.ps1" goto promoted

echo [FAIL] Neither ISOLATED_COPY.flag nor my_portal_ctl.ps1 exists in %~dp0
echo        Refusing to guess which mode this directory is in.
exit /b 2

:isolated
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_all.ps1" %*
exit /b %errorlevel%

:promoted
if not "%~1"=="" (
    echo [FAIL] Promoted copy: switch arguments are isolated-only ^(-DryRun / -SkipNginxConf^).
    echo        Refusing to start with: %*
    exit /b 2
)
echo [*] Promoted copy (no ISOLATED_COPY.flag): my_portal_ctl.ps1 8-start-all
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0my_portal_ctl.ps1" 8-start-all
exit /b %errorlevel%
