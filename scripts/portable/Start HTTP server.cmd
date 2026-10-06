@echo off
rem Serves CSiBridge over HTTP on this machine (port 8765), for remote clients that hold an access token.
rem Create tokens first with "Add access token.cmd". Keep this window open while the server runs.
"%~dp0python\python.exe" -m csibridge_mcp serve --http %*
echo.
pause
