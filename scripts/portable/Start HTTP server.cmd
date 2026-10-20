@echo off
rem Serves CSiBridge over HTTP on this machine (port 8765), for remote clients that hold an access token.
rem Create tokens first with "Add access token.cmd". Keep this window open while the server runs.
if not exist "%~dp0python\python.exe" (
    echo This file only works inside the unzipped csibridge-mcp folder. Right-click the
    echo zip, choose Extract All, then run it from the extracted folder.
    pause
    exit /b 1
)
"%~dp0python\python.exe" -m csibridge_mcp serve --http %*
echo.
pause
