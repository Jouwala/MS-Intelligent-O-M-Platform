@echo off
REM ============================================================
REM  OLBR production nginx - READ-ONLY check
REM
REM  Two things this file deliberately does NOT do:
REM
REM  1) It does not regenerate nginx.conf. The generator
REM     (ops\deploy\write_nginx_conf.ps1) takes ports from the tree's
REM     .env, and this tree's .env still carries the isolated-copy
REM     ports (8080/8443/7039/7091/9277) while production runs
REM     80/443/7038/7090/9267. Regenerating here would overwrite the
REM     live config with 8080/8443 - the old 5-nginx-check.bat did
REM     exactly that.
REM  2) It does not run "nginx -t" while nginx is up: on Windows the
REM     running master holds logs\access.log, so the test dies with
REM     'CreateFile() "...access.log" failed (5)' even when the config
REM     is fine. Validate syntax while the stack is stopped.
REM
REM  What it does show: the live conf path/mtime, its listen
REM  directives, and whether those ports are actually listening.
REM  ASCII-only + CRLF on purpose; non-ASCII breaks cmd parsing.
REM ============================================================
setlocal
for %%F in ("%~dp0..\..\nginx-1.28.0") do set "NGX=%%~fF"
if not exist "%NGX%\conf\nginx.conf" (
  echo nginx.conf not found: %NGX%\conf\nginx.conf
  goto :done
)
echo Config   : %NGX%\conf\nginx.conf
for %%F in ("%NGX%\conf\nginx.conf") do echo Written  : %%~tF
echo.
echo listen directives:
findstr /r /c:"^ *listen" "%NGX%\conf\nginx.conf"
echo.
echo listening now:
for %%P in (80 443) do call :showport %%P
goto :done

:showport
netstat -ano | findstr /r /c:":%~1 .*LISTENING" >nul
if errorlevel 1 (echo    %~1  not listening) else (echo    %~1  LISTEN)
goto :eof

:done
echo.
echo ---- done. Press any key to close. ----
pause >nul
endlocal
