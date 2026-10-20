@echo off
rem Checks that this folder can talk to the CSiBridge you have open. Changes nothing in your model.
if not exist "%~dp0python\python.exe" (
    echo This file only works inside the unzipped csibridge-mcp folder. Right-click the
    echo zip, choose Extract All, then run it from the extracted folder.
    pause
    exit /b 1
)
"%~dp0python\python.exe" -m csibridge_mcp selftest
echo.
pause
