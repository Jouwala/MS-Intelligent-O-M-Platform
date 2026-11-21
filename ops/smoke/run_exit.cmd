@echo off
REM run_exit.cmd - helper for ops\smoke\smoke.ps1
REM Runs a PowerShell script with stdout/stderr redirected to FILES (not pipes),
REM so long-lived children started by start_all.ps1 (uvicorn/vite/portal/nginx)
REM cannot hold a pipe open; writes the exit code to a 4th file when done.
REM usage: run_exit.cmd <script.ps1> <stdout file> <stderr file> <exit-code file>
powershell -NoProfile -ExecutionPolicy Bypass -File "%~1" 1>"%~2" 2>"%~3"
REM redirect written BEFORE echo: 'echo 3>file' would redirect handle 3 and write nothing
>"%~4" echo %ERRORLEVEL%
