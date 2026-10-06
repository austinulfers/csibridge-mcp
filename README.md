# csibridge-mcp

An MCP server that lets Claude Code drive a running [CSiBridge](https://www.csiamerica.com/products/csibridge):
build and edit models, run analyses, and read results, by calling CSiBridge's own API.

## How it works

CSiBridge exposes its API through COM, which exists only on Windows. So the server runs on the
same Windows machine as CSiBridge and Claude Code, attaches to the CSiBridge instance you already
have open, and works in that live session: you see every change on screen as it happens.

```
Claude Code  --stdio-->  csibridge-mcp  --COM (comtypes)-->  CSiBridge
```

Rather than wrapping a hand-picked subset of the API, the server exposes all of it:

- **`call` / `batch`** invoke any API function by its documented path, such as
  `SapModel.FrameObj.AddByCoord`.
- **`api_search` / `api_describe` / `api_enum`** read function signatures and enum values from the
  type library of the CSiBridge version that is actually installed, so Claude checks a signature
  instead of guessing it. This includes the parametric Bridge Modeler API
  (`SapModel.BridgeModeler_1`, CSiBridge v25 and later).
- **`list_tables` / `get_table` / `edit_table`** read and edit CSiBridge's database tables, which
  cover model definitions (including bridge layout lines, deck sections and bridge objects) and all
  analysis and design results.

Because CSiBridge cannot run on macOS or Linux, the server also has a **mock backend**: a small fake
of the API, shaped like the real COM wrappers, that makes it possible to develop and test here.

## Setup on Windows

Requirements: CSiBridge installed and licensed, [Claude Code](https://claude.com/claude-code), and
[uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`).

1. Put this folder on the Windows machine and install its dependencies:

   ```powershell
   cd C:\path\to\csibridge-mcp
   uv sync
   ```

2. Start CSiBridge, then check that the server can reach it:

   ```powershell
   uv run csibridge-mcp selftest
   ```

   This attaches to CSiBridge, reads the API's type library, makes a few read-only calls and prints
   a pass/fail report. It changes nothing in your model.

3. Register the server with Claude Code:

   ```powershell
   claude mcp add --scope user csibridge -- uv run --directory C:\path\to\csibridge-mcp csibridge-mcp
   ```

4. In Claude Code, run `/mcp` to confirm `csibridge` is connected, then try
   "What model is open in CSiBridge?"

Notes:

- CSiBridge and Claude Code must run as the same Windows user at the same elevation (both normal,
  or both "as administrator"); otherwise the running instance cannot be found.
- The server attaches to a running CSiBridge. It only starts CSiBridge when asked to
  (`connect` with `mode="launch"`); pass `--no-launch` to forbid that.
- The first call after installing is slow, because comtypes generates Python wrappers for the whole
  API once and caches them.
- Python must be 64-bit, like CSiBridge. That is what uv installs by default.
- `--helper-progid` and `--progid` select a different CSI API library or product object. The
  defaults are `CSiAPIv1.Helper` and `CSI.CSiBridge.API.SapObject`.

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

## Developing on macOS or Linux

```bash
uv sync
uv run pytest                          # the test suite, against the mock backend
uv run csibridge-mcp selftest --mock   # the same report the Windows self-test prints
```

Opening this folder in Claude Code offers a `csibridge-mock` server (see `.mcp.json`), which is the
real MCP server running on the mock backend, so the tools can be tried without Windows.

The mock (`src/csibridge_mcp/mock_csi.py`) implements a few dozen functions: enough to build a small
frame model, "analyse" it and read tables. Its numbers are placeholders, not structural results.

Code layout, in `src/csibridge_mcp/`:

| File | Role |
| --- | --- |
| `server.py` | The MCP tools |
| `engine.py` | Owns the CSiBridge connection; runs requests one at a time on a worker thread; the real (COM) and mock backends; the self-test |
| `introspect.py` | Reads function signatures from the type library; converts arguments and labels results |
| `mock_csi.py` | The fake API used off Windows |
| `cli.py` | Command line entry point |

## What has and has not been verified

This was developed on a Mac, where CSiBridge cannot run.

- Verified: the MCP server and every tool, end to end over stdio, against the mock backend
  (`uv run pytest`).
- Verified: the assumptions the real backend makes about comtypes, by running the introspection and
  result-labelling code against comtypes' own member-spec and calling-convention code
  (`tests/test_comtypes_contract.py`).
- **Not yet verified: anything against a real CSiBridge.** The COM backend in `engine.py` follows
  CSI's published Python example and comtypes' source, but it has never been executed. The three
  table tools also rely on the argument order of the `SapModel.DatabaseTables` functions as CSI
  documents them.

So the first run on Windows is the real test. Run `uv run csibridge-mcp selftest` there first; its
report shows which layer fails, if any.

## License

[Functional Source License 1.1, MIT future license](LICENSE.md) (FSL-1.1-MIT). You may use,
modify and share it freely, including inside a company and for paid engineering work; what you may
not do is offer it to others as a competing commercial product or service. Each version becomes
MIT-licensed two years after its release.
