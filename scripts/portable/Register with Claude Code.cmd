@echo off
rem Adds this folder's server to Claude Code (for your user account, so it works in every project).
claude mcp add --scope user csibridge -- "%~dp0python\python.exe" -m csibridge_mcp
echo.
echo If that worked, run /mcp inside Claude Code to confirm "csibridge" is connected.
pause
