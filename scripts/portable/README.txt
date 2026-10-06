csibridge-mcp, portable edition
================================

Lets an AI assistant (Claude Code, GitHub Copilot, Microsoft 365 Copilot and
other MCP clients) drive the CSiBridge you have open on this computer.

This folder contains everything it needs, including its own Python in the
"python" subfolder. Nothing is installed and no admin rights are required:
unzip it anywhere (for example in Documents) and use the files below.

  Self-test.cmd
      Start CSiBridge first, then double-click this. It checks that the
      server can see CSiBridge and prints a report. It changes nothing.

  Register with Claude Code.cmd
      Adds the server to Claude Code. Afterwards, run /mcp inside Claude
      Code to confirm it is connected, then ask about your model.

  For VS Code (GitHub Copilot), add a server of type "stdio" whose command
  is the python.exe in this folder's "python" subfolder with the arguments
  "-m" and "csibridge_mcp". The main README on GitHub shows the exact JSON.

  Add access token.cmd  /  Start HTTP server.cmd
      Only for sharing the server with remote clients such as Microsoft 365
      Copilot. Create a token for each person first, then start the server.
      Anyone without a token is refused. See the GitHub README for how to
      publish the server over HTTPS.

  csibridge-mcp.cmd
      The full command line, for anything else (csibridge-mcp.cmd --help).

Notes
  - CSiBridge and the assistant must run as the same Windows user, at the
    same elevation (both normal, or both "as administrator").
  - The first use is slow while Python wrappers for the CSiBridge API are
    generated and cached.

Project, documentation and license: https://github.com/austinulfers/csibridge-mcp
