@echo off
rem Adds this folder's server to Claude Code (for your user account, so it works in every project).
if not exist "%~dp0python\python.exe" (
    echo This file only works inside the unzipped csibridge-mcp folder. Right-click the
    echo zip, choose Extract All, then run it from the extracted folder.
    pause
    exit /b 1
)
where claude >nul 2>nul || (
    echo Claude Code's "claude" command was not found. Install Claude Code from
    echo https://claude.com/claude-code, then run this file again.
    echo.
    pause
    exit /b 1
)
rem "call", because an npm install of Claude Code is itself a batch file (claude.cmd) and would not return here.
call claude mcp add --scope user csibridge -- "%~dp0python\python.exe" -m csibridge_mcp
echo.
if errorlevel 1 (
    echo Claude Code did not add the server: see the message above.
) else (
    echo Done. Run /mcp inside Claude Code to confirm "csibridge" is connected.
)
pause
