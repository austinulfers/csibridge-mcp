@echo off
rem Creates an access token for one person, for use with "Start HTTP server.cmd".
set /p NAME=Who is this token for (a name or email address)?
"%~dp0python\python.exe" -m csibridge_mcp token add "%NAME%"
echo.
pause
