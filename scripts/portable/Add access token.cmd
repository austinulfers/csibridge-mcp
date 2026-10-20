@echo off
rem Creates an access token for one person, for use with "Start HTTP server.cmd".
if not exist "%~dp0python\python.exe" (
    echo This file only works inside the unzipped csibridge-mcp folder. Right-click the
    echo zip, choose Extract All, then run it from the extracted folder.
    pause
    exit /b 1
)
set /p NAME=Who is this token for (a name or email address)?
"%~dp0python\python.exe" -m csibridge_mcp token add "%NAME%"
echo.
pause
