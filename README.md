# csibridge-mcp

An MCP server that lets an AI assistant drive a running
[CSiBridge](https://www.csiamerica.com/products/csibridge): build and edit models, run analyses, and
read results, by calling CSiBridge's own API. It works with any client that speaks the
[Model Context Protocol](https://modelcontextprotocol.io): Claude Code, GitHub Copilot in VS Code or
Visual Studio, and, over HTTP, remote clients such as Microsoft 365 Copilot.

## How it works

CSiBridge exposes its API through COM, which exists only on Windows. So the server runs on the
same Windows machine as CSiBridge, attaches to the CSiBridge instance you already have open, and
works in that live session: you see every change on screen as it happens.

```
MCP client  --stdio or HTTPS-->  csibridge-mcp  --COM (comtypes)-->  CSiBridge
```

Rather than wrapping a hand-picked subset of the API, the server exposes all of it:

- **`call` / `batch`** invoke any API function by its documented path, such as
  `SapModel.FrameObj.AddByCoord`.
- **`api_search` / `api_describe` / `api_enum`** read function signatures and enum values from the
  type library of the CSiBridge version that is actually installed, so the assistant checks a
  signature instead of guessing it. This includes the parametric Bridge Modeler API
  (`SapModel.BridgeModeler_1`, CSiBridge v25 and later).
- **`list_tables` / `get_table` / `edit_table`** read and edit CSiBridge's database tables, which
  cover model definitions (including bridge layout lines, deck sections and bridge objects) and all
  analysis and design results.

Because CSiBridge cannot run on macOS or Linux, the server also has a **mock backend**: a small fake
of the API, shaped like the real COM wrappers, that makes it possible to develop and test here.

## Setup on Windows

Requirements: CSiBridge installed and licensed, and an MCP client. Then pick one of two ways to get
the server onto the machine.

### Option A: the portable zip (nothing to install)

For locked-down or non-technical machines. The zip bundles the official embeddable Python, so it
needs no Python, uv, git or admin rights.

1. Download `csibridge-mcp-portable-win64.zip` from the
   [releases page](https://github.com/austinulfers/csibridge-mcp/releases) (every CI run also
   produces it as a build artifact) and unzip it anywhere, for example in Documents.
2. Start CSiBridge, then double-click **`Self-test.cmd`**. It attaches to CSiBridge, reads the API's
   type library, makes a few read-only calls and prints a pass/fail report. It changes nothing in
   your model.
3. Connect your client (see below). The server command is the `python\python.exe` inside the folder
   with the arguments `-m csibridge_mcp`; **`Register with Claude Code.cmd`** does this for Claude Code.

### Option B: from source with uv

Needs [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`).

```powershell
git clone https://github.com/austinulfers/csibridge-mcp.git
cd csibridge-mcp
uv sync
uv run csibridge-mcp selftest
```

The server command is then `uv run --directory C:\path\to\csibridge-mcp csibridge-mcp`.

### Connecting a client on the same machine

**Claude Code**

```powershell
claude mcp add --scope user csibridge -- uv run --directory C:\path\to\csibridge-mcp csibridge-mcp
```

(Or the portable folder's `python\python.exe -m csibridge_mcp` in place of the `uv run ...` part.)
Run `/mcp` inside Claude Code to confirm `csibridge` is connected.

**VS Code (GitHub Copilot agent mode)** — add the server with the "MCP: Add Server" command, or
put this in `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "csibridge": {
      "type": "stdio",
      "command": "C:\\path\\to\\csibridge-mcp\\python\\python.exe",
      "args": ["-m", "csibridge_mcp"]
    }
  }
}
```

**Visual Studio 2022 (17.14 or later)** — the same `servers` block in a `.mcp.json` next to your
solution.

Then ask the assistant something like "What model is open in CSiBridge?"

Notes:

- CSiBridge and the MCP client must run as the same Windows user at the same elevation (both
  normal, or both "as administrator"); otherwise the running instance cannot be found.
- The server attaches to a running CSiBridge. It only starts CSiBridge when asked to
  (`connect` with `mode="launch"`); pass `--no-launch` to forbid that.
- The first call after installing is slow, because comtypes generates Python wrappers for the whole
  API once and caches them.
- Python must be 64-bit, like CSiBridge. The portable zip and uv's default both are.
- `--helper-progid` and `--progid` select a different CSI API library or product object. The
  defaults are `CSiAPIv1.Helper` and `CSI.CSiBridge.API.SapObject`.

## Remote access over HTTP (Microsoft 365 Copilot, online agents)

Clients that run in the cloud cannot start a program on your PC, so for them the server can be
served over HTTP instead of stdio. Access is locked down with **per-person tokens**: the server
refuses every request that does not carry a valid one, tokens are issued and revoked by whoever
hosts the server, and each request is logged under the name the token was issued to.

1. Create a token for each person (or client) allowed in. The token is shown once; it is stored
   hashed in `~/.csibridge-mcp/tokens.json`.

   ```powershell
   uv run csibridge-mcp token add "Jane Doe"
   ```

   (`token list` and `token revoke "Jane Doe"` manage them; revoking takes effect immediately on a
   running server. The portable folder has `Add access token.cmd` for this.)

2. Start the server over HTTP. With CSiBridge open:

   ```powershell
   uv run csibridge-mcp serve --http
   ```

   (Portable folder: `Start HTTP server.cmd`.) By default it listens only on this machine, at
   `http://localhost:8765/mcp`, and refuses to start if no tokens exist.

3. Make it reachable over **HTTPS** — never expose it as plain HTTP. Any of these work:

   - A tunnel, which also handles certificates and firewalls:
     `cloudflared tunnel --url http://localhost:8765` prints a public `https://…` address.
   - A reverse proxy (IIS, Caddy, nginx) that terminates TLS and forwards to `localhost:8765`.
   - Direct, if you have a certificate: `serve --http --host 0.0.0.0 --tls-cert cert.pem --tls-key key.pem`.

4. Point the client at `https://<your address>/mcp` with the token as a header, either
   `Authorization: Bearer <token>` or `X-API-Key: <token>`:

   - **Microsoft 365 Copilot** — in Copilot Studio, add a tool of type *Model Context Protocol*
     with that URL, authentication *API key*, sent in the `X-API-Key` header. Share the resulting
     agent only with the people who should have it: that, plus the token, is the lock-down.
   - **Claude Code** (from another machine):
     `claude mcp add --transport http csibridge https://<your address>/mcp --header "Authorization: Bearer <token>"`
   - **VS Code**: `"type": "http", "url": "https://<your address>/mcp", "headers": {"X-API-Key": "<token>"}`.

Things to understand before sharing a token:

- A token gives full use of the CSiBridge session on the hosting machine, including saving files
  there as the hosting user. Issue tokens only to people you would let sit at that computer.
- Everyone with a token works in the **same** CSiBridge session and model. Calls run one at a time.
- Claude.ai and ChatGPT custom connectors require OAuth sign-in rather than a static token, so
  they cannot use this server yet.

## Tools

| Tool | Purpose |
| --- | --- |
| `status` | Whether CSiBridge is attached, the open model, units, lock state, anything still running |
| `connect` | Attach to a specific instance, re-attach, or launch CSiBridge |
| `call` | Call any API function by path |
| `batch` | Run many calls in one round trip |
| `api_search` | Find functions by name; returns full signatures |
| `api_describe` | List an interface's functions, or show one function's exact signature |
| `api_enum` | List the values of an enum such as `eUnits` |
| `open_model`, `save_model`, `new_model` | Model files |
| `run_analysis` | Run the analysis |
| `job_wait` | Keep waiting for a long operation |
| `list_tables`, `get_table`, `edit_table` | Database tables: definitions and results |

Conventions worth knowing:

- A call returns `{"ret": ..., "outputs": {...}, "ok": ...}`. `outputs` holds the function's `ref`
  parameters by name. For most functions `ret` is a status code where 0 means success, and `ok` is
  false when it is nonzero.
- `ref` parameters can be left out of a call, and arguments can be given by name.
- An operation that outlasts its `wait_seconds` (an analysis, typically) returns
  `{"pending": true, "job_id": ...}` and keeps running; `job_wait` collects the result.
- Calls run one at a time, in order, because the CSI API is not re-entrant.
- The server sends clients a short set of usage instructions (conventions like the ones above).
  Clients that do not pass server instructions to the model still get the essentials, which are
  repeated in each tool's description.

## Developing on macOS or Linux

```bash
uv sync
uv run pytest                          # the test suite, against the mock backend
uv run csibridge-mcp selftest --mock   # the same report the Windows self-test prints
uv run csibridge-mcp serve --http --mock --token csib_dev   # the HTTP transport, locally
```

This folder carries a `csibridge-mock` server entry for Claude Code (`.mcp.json`) and for VS Code
(`.vscode/mcp.json`): the real MCP server running on the mock backend, so the tools can be tried
in either client without Windows.

The mock (`src/csibridge_mcp/mock_csi.py`) implements a few dozen functions: enough to build a small
frame model, "analyse" it and read tables. Its numbers are placeholders, not structural results.

Code layout, in `src/csibridge_mcp/`:

| File | Role |
| --- | --- |
| `server.py` | The MCP tools |
| `engine.py` | Owns the CSiBridge connection; runs requests one at a time on a worker thread; the real (COM) and mock backends; the self-test |
| `introspect.py` | Reads function signatures from the type library; converts arguments and labels results |
| `access.py` | Access tokens and the auth layer for the HTTP transport |
| `mock_csi.py` | The fake API used off Windows |
| `cli.py` | Command line entry point |

The portable zip is built by `scripts/build-portable.ps1` (launchers in `scripts/portable/`); CI
(`.github/workflows/ci.yml`) runs the tests on Windows, macOS and Linux, builds the zip on every
push, and attaches it to the release for tags named `v*`.

## What has and has not been verified

This was developed on a Mac, where CSiBridge cannot run.

- Verified: the MCP server and every tool, end to end over stdio and over HTTP (including the token
  check), against the mock backend (`uv run pytest`).
- Verified: the assumptions the real backend makes about comtypes, by running the introspection and
  result-labelling code against comtypes' own member-spec and calling-convention code
  (`tests/test_comtypes_contract.py`). CI on Windows additionally checks that the COM path gets as
  far as looking for CSiBridge and reports its absence cleanly.
- **Not yet verified: anything against a real CSiBridge.** The COM backend in `engine.py` follows
  CSI's published Python example and comtypes' source, but it has never been executed against the
  program. The three table tools also rely on the argument order of the `SapModel.DatabaseTables`
  functions as CSI documents them.

So the first run on a machine with CSiBridge is the real test. Run the self-test there first; its
report shows which layer fails, if any.

## License

[Functional Source License 1.1, MIT future license](LICENSE.md) (FSL-1.1-MIT). You may use,
modify and share it freely, including inside a company and for paid engineering work; what you may
not do is offer it to others as a competing commercial product or service. Each version becomes
MIT-licensed two years after its release.
