@echo off
rem Checks that this folder can talk to the CSiBridge you have open. Changes nothing in your model.
"%~dp0python\python.exe" -m csibridge_mcp selftest
echo.
pause
