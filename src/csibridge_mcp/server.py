"""The MCP server: the tools an MCP client uses to drive CSiBridge."""
from __future__ import annotations

import json
from typing import Any, Literal

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .engine import DEFAULT_WAIT, UNITS, Engine
from .errors import CsiError

INSTRUCTIONS = """\
Drives a running CSiBridge (CSI's bridge analysis and design program) through its API.

Calling the API
- Every CSiBridge API function is reachable with `call`, addressed by its documented path, e.g. \
SapModel.FrameObj.AddByCoord. `batch` runs many calls in one round trip.
- Signatures come from the installed CSiBridge's own type library. Use `api_search` / `api_describe` \
before calling a function for the first time rather than guessing its parameter order.
- Parameters marked `ref` are outputs (sometimes also inputs). They can be left out, and inputs can be \
passed by name; results come back by name under `outputs`. `ret` is the function's return value: for \
most functions a status code where 0 means success.
- Enum parameters are integers; a member name such as "eUnits.kN_m_C" is accepted too. `api_enum` lists values.

Working with a model
- Call `status` first: it shows whether CSiBridge is attached, the open model, present units and lock state.
- Numbers are in the model's present units. Change them with SapModel.SetPresentUnits.
- After an analysis the model is locked. Editing it requires SapModel.SetModelIsLocked(false), which \
discards the results.
- Database tables (`list_tables`, `get_table`, `edit_table`) reach nearly everything, including Bridge \
Modeler definitions (layout lines, deck sections, bridge objects) and all analysis and design results. \
Prefer them for bulk reads.
- The parametric Bridge Modeler API is under SapModel.BridgeModeler_1 (CSiBridge v25 and later).
- Long operations such as an analysis may return `pending` with a job_id; keep waiting with `job_wait`.
- File paths are Windows paths on the machine running CSiBridge.
- Everything happens in the user's live CSiBridge session. Opening or creating a model discards unsaved \
changes to the current one, so save first, and confirm before replacing or overwriting a model the user \
did not ask you to touch.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True)
DESTRUCTIVE = ToolAnnotations(destructiveHint=True)
TABLES = "SapModel.DatabaseTables."
PENDING_NOTE = "Still running. Call job_wait with this job_id to keep waiting; other calls queue behind it."


def _render(result: dict) -> str:
    if result.get("pending"):
        result = {**result, "note": PENDING_NOTE}
    return json.dumps(result, ensure_ascii=False)


def _outputs(result: dict) -> list:
    """A call's ref outputs in declaration order."""
    if "outputs" in result:
        return list(result["outputs"].values())
    raw = result.get("raw")  # no type information was available to label them
    return list(raw[:-1]) if isinstance(raw, list) else []


def _cell(value: Any) -> str:
    """Database tables are all text; render a JSON value as a cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


MCP_PATH = "/mcp"


def create_server(engine: Engine, http: bool = False) -> FastMCP:
    """Build the MCP server; ``http`` tunes it for the streamable HTTP transport."""
    transport = {}
    if http:
        transport = {
            # Nothing here depends on MCP session state (the engine is shared),
            # and stateless JSON responses work with the widest range of
            # remote clients, tunnels and proxies.
            "stateless_http": True,
            "json_response": True,
            "streamable_http_path": MCP_PATH,
            # The SDK's Host-header check guards unauthenticated local servers
            # against DNS rebinding. Here every request is token-checked, and
            # behind a tunnel or proxy the Host header is not predictable.
            "transport_security": TransportSecuritySettings(enable_dns_rebinding_protection=False),
        }
    mcp = FastMCP("csibridge", instructions=INSTRUCTIONS, log_level="WARNING", **transport)

    async def request(op: str, params: dict | None = None, wait: float = DEFAULT_WAIT) -> dict:
        try:
            return await anyio.to_thread.run_sync(engine.request, op, params or {}, wait)
        except CsiError as exc:
            raise ToolError(exc.message) from exc

    def require_idle() -> None:
        """Multi-step tools must not queue half their steps behind a long job."""
        if (busy := engine.busy()) is not None:
            raise ToolError(
                f"CSiBridge is busy running {busy['label']} ({busy['running_seconds']}s so far). "
                f"Wait for it with job_wait(job_id='{busy['job_id']}') and try again."
            )

    async def api(method: str, *args: Any, failure: str | None = None) -> list:
        """Call one API function that must succeed; return its ref outputs."""
        result = await request("call", {"method": method, "args": list(args)})
        if result.get("pending"):
            raise ToolError(
                f"{method} is still running. Wait for it with job_wait(job_id='{result['job_id']}') and try again."
            )
        if not result["ok"]:
            raise ToolError(f"{failure or method + ' failed'} ({result['error']})")
        return _outputs(result)

    # -- session ----------------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    async def status() -> str:
        """Show whether CSiBridge is attached and what is open in it.

        Reports the program version, the open model file, present units, whether the model is
        locked, and whether a long operation is still running. Call this first.
        """
        result = await request("status")
        if result.get("backend") == "mock":
            result["note"] = "MOCK backend: a fake in-memory model for development, not CSiBridge."
        return _render(result)

    @mcp.tool()
    async def connect(
        mode: Literal["attach", "launch"] = "attach",
        pid: int | None = None,
        program_path: str | None = None,
    ) -> str:
        """Attach to a running CSiBridge, or start one.

        The other tools attach to a running CSiBridge automatically, so this is only needed to
        start CSiBridge (mode="launch"), to choose between several running instances (pid), or to
        re-attach after CSiBridge was restarted.

        Args:
            mode: "attach" to use a running instance, "launch" to start a new one.
            pid: With "attach", the process id of the instance to use.
            program_path: With "launch", the full path of a specific CSiBridge.exe. Defaults to
                the latest installed version.
        """
        params = {"mode": mode, "pid": pid, "program_path": program_path}
        return _render(await request("connect", params, wait=180))

    # -- the API itself ---------------------------------------------------

    @mcp.tool()
    async def call(
        method: str,
        args: list[Any] | dict[str, Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        wait_seconds: float = DEFAULT_WAIT,
    ) -> str:
        """Call any CSiBridge API function by its documented path.

        Returns {"ret": return value, "outputs": {ref parameter: value}, "ok": bool}. For most
        functions `ret` is a status code and 0 means success. Check a function's signature with
        api_describe or api_search before calling it for the first time.

        Args:
            method: The function path, e.g. "SapModel.FrameObj.AddByCoord".
            args: Input arguments in signature order. Trailing optional and `ref` parameters may
                be left off. An object of name: value is also accepted.
            kwargs: Input arguments by parameter name, instead of or after positional ones.
            wait_seconds: How long to wait before returning a pending job_id instead.
        """
        result = await request("call", {"method": method, "args": args, "kwargs": kwargs}, wait_seconds)
        return _render(result)

    @mcp.tool()
    async def batch(calls: list[dict[str, Any]], stop_on_error: bool = True, wait_seconds: float = DEFAULT_WAIT) -> str:
        """Run many API calls in order in one round trip.

        Use this to build or query a model efficiently (for example adding dozens of objects).

        Args:
            calls: [{"method": "...", "args": [...], "kwargs": {...}}, ...], each with the same
                meaning as the `call` tool.
            stop_on_error: Stop at the first call that fails (default) rather than continuing.
            wait_seconds: How long to wait before returning a pending job_id instead.
        """
        result = await request("batch", {"calls": calls, "stop_on_error": stop_on_error}, wait_seconds)
        return _render(result)

    @mcp.tool(annotations=READ_ONLY)
    async def api_search(query: str, limit: int = 30) -> str:
        """Search the installed CSiBridge API for functions by name.

        Matches every word of the query against function paths and parameter names, and returns
        full signatures, e.g. query "frame distributed load" or "BridgeModeler span".
        """
        return _render(await request("search", {"query": query, "limit": limit}))

    @mcp.tool(annotations=READ_ONLY)
    async def api_describe(path: str = "", filter: str = "") -> str:
        """Describe part of the CSiBridge API, read from the installed version's type library.

        Args:
            path: A function path ("SapModel.FrameObj.AddByCoord") for its exact parameters, an
                interface path ("SapModel.FrameObj") to list its functions, or empty for the tree
                of all interfaces.
            filter: When listing an interface, only show functions whose name contains this text.
        """
        return _render(await request("describe", {"path": path, "filter": filter}))

    @mcp.tool(annotations=READ_ONLY)
    async def api_enum(name: str = "") -> str:
        """List the values of an API enum such as eUnits, eMatType or eLoadPatternType.

        Give an enum name, or part of an enum or member name to find it. Empty lists all enums.
        """
        return _render(await request("enums", {"name": name}))

    @mcp.tool(annotations=READ_ONLY)
    async def job_wait(job_id: str, wait_seconds: float = DEFAULT_WAIT) -> str:
        """Wait for a pending operation (one that returned a job_id) and return its result."""
        return _render(await request("job", {"job_id": job_id}, wait_seconds))

    # -- models and analysis ----------------------------------------------

    @mcp.tool(annotations=DESTRUCTIVE)
    async def open_model(path: str) -> str:
        """Open a CSiBridge model file (.bdb), replacing the model currently open.

        Unsaved changes to the current model are lost, so save it first if they matter.

        Args:
            path: Full Windows path of the model on the machine running CSiBridge.
        """
        result = await request("call", {"method": "SapModel.File.OpenFile", "args": [path]}, wait=180)
        if result.get("ok") is False:
            result["hint"] = "Check that the path exists on the CSiBridge machine and is a CSiBridge model."
        return _render(result)

    @mcp.tool()
    async def save_model(path: str = "") -> str:
        """Save the open model.

        Args:
            path: Full Windows path (.bdb) to save as. Empty saves to the model's current file,
                which fails if the model has never been saved.
        """
        return _render(await request("call", {"method": "SapModel.File.Save", "args": [path]}, wait=180))

    @mcp.tool(annotations=DESTRUCTIVE)
    async def new_model(units: str = "kN_m_C") -> str:
        """Start a new blank model, replacing the model currently open.

        Unsaved changes to the current model are lost, so save it first if they matter.

        Args:
            units: Present units for the new model, one of: lb_in_F, lb_ft_F, kip_in_F, kip_ft_F,
                kN_mm_C, kN_m_C, kgf_mm_C, kgf_m_C, N_mm_C, N_m_C, Ton_mm_C, Ton_m_C, kN_cm_C,
                kgf_cm_C, N_cm_C, Ton_cm_C.
        """
        codes = {name.lower(): code for code, name in UNITS.items()}
        if units.lower() not in codes:
            raise ToolError(f"Unknown units {units!r}. Use one of: {', '.join(UNITS.values())}.")
        require_idle()
        await api("SapModel.InitializeNewModel", codes[units.lower()])
        await api("SapModel.File.NewBlank")
        return _render({"ok": True, "units": UNITS[codes[units.lower()]], "note": "New blank model; not yet saved."})

    @mcp.tool()
    async def run_analysis(wait_seconds: float = DEFAULT_WAIT) -> str:
        """Run the analysis of the open model.

        The model must already have been saved to a file. Running locks the model. Large models
        take a while: if this returns pending, keep waiting with job_wait.
        """
        result = await request("call", {"method": "SapModel.Analyze.RunAnalysis"}, wait_seconds)
        if result.get("ok") is False:
            result["hint"] = "The model must be saved to a file and contain something to analyse."
        return _render(result)

    # -- database tables --------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    async def list_tables(filter: str = "", include_empty: bool = False, limit: int = 200) -> str:
        """List CSiBridge database tables: model definitions and analysis/design results.

        Each entry has the table key (used by get_table) and its import_type: 0 = read-only,
        1 = editable only by file import, 2 = editable with edit_table while the model is
        unlocked, 3 = editable with edit_table at any time.

        Args:
            filter: Only tables whose key contains every word, e.g. "bridge section" or "joint".
            include_empty: Also list tables that have no data in the current model.
            limit: Maximum number of tables to return.
        """
        require_idle()
        if include_empty:
            _, keys, _, import_types, empty = await api(TABLES + "GetAllTables")
        else:
            _, keys, _, import_types = await api(TABLES + "GetAvailableTables")
            empty = [False] * len(keys)
        words = filter.lower().split()
        tables = [
            {"key": key, "import_type": import_type, **({"empty": True} if is_empty else {})}
            for key, import_type, is_empty in zip(keys, import_types, empty)
            if all(word in key.lower() for word in words)
        ]
        return _render({"total": len(tables), "returned": min(len(tables), limit), "tables": tables[:limit]})

    @mcp.tool(annotations=READ_ONLY)
    async def get_table(
        table_key: str,
        fields: list[str] | None = None,
        group: str = "",
        where: dict[str, str] | None = None,
        limit: int = 100,
        offset: int = 0,
        for_editing: bool = False,
        load_cases: list[str] | None = None,
        load_combos: list[str] | None = None,
        load_patterns: list[str] | None = None,
    ) -> str:
        """Read a CSiBridge database table as {"fields": [...], "rows": [[...], ...]}.

        Works for model definition tables and, after an analysis, result tables. All values are
        text, in the model's present units.

        Args:
            table_key: The table's key, from list_tables.
            fields: Only these fields. Default: all fields.
            group: Only objects in this group. Default: all objects.
            where: Only rows whose fields equal these values, e.g. {"OutputCase": "DEAD"}.
            limit: Maximum number of rows to return.
            offset: Number of matching rows to skip, for paging.
            for_editing: Read the table in the form edit_table expects (all fields, plus the
                table_version to pass back).
            load_cases: For result tables, the load cases to report.
            load_combos: For result tables, the load combinations to report.
            load_patterns: For result tables, the load patterns to report.
        """
        require_idle()
        selections = {
            "SetLoadCasesSelectedForDisplay": load_cases,
            "SetLoadCombinationsSelectedForDisplay": load_combos,
            "SetLoadPatternsSelectedForDisplay": load_patterns,
        }
        for setter, names in selections.items():
            if names is not None:
                await api(TABLES + setter, names)
        failure = (
            f"Could not read table {table_key!r}. Check the key with list_tables; "
            "result tables also need a completed analysis"
        )
        if for_editing:
            version, names, _, flat = await api(TABLES + "GetTableForEditingArray", table_key, group, failure=failure)
        else:
            _, version, names, _, flat = await api(
                TABLES + "GetTableForDisplayArray", table_key, fields or [], group, failure=failure
            )
        width = len(names)
        rows = [flat[i : i + width] for i in range(0, len(flat), width)] if width else []
        if where:
            if unknown := [name for name in where if name not in names]:
                raise ToolError(f"Table {table_key!r} has no field {unknown[0]!r}. Fields: {', '.join(names)}.")
            columns = {names.index(name): str(value) for name, value in where.items()}
            rows = [row for row in rows if all(row[i] == value for i, value in columns.items())]
        page = rows[offset : offset + limit]
        result = {
            "table": table_key,
            "table_version": version,
            "fields": names,
            "total_rows": len(rows),
            "offset": offset,
            "returned": len(page),
            "rows": page,
        }
        if offset + len(page) < len(rows):
            result["note"] = f"More rows remain; continue with offset={offset + len(page)}."
        return _render(result)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def edit_table(
        table_key: str,
        fields: list[str],
        rows: list[Any],
        table_version: int | None = None,
        apply: bool = True,
    ) -> str:
        """Replace the contents of an editable database table (CSiBridge's interactive database editing).

        The rows given become the table's entire contents: records left out are removed from the
        model. So read the table with get_table(for_editing=True), change what is needed, and send
        every row back. The model must be unlocked for most tables. Returns CSiBridge's import log.

        Args:
            table_key: The table's key, from list_tables.
            fields: Field keys, in the order used by each row.
            rows: The records, each a list of values in `fields` order or an object keyed by field.
            table_version: The table_version returned by get_table(for_editing=True). Looked up if omitted.
            apply: Apply the edit to the model now. With false the edit is only staged, to be
                applied together with other tables by a later edit_table call.
        """
        require_idle()
        flat: list[str] = []
        for number, row in enumerate(rows, start=1):
            if isinstance(row, dict):
                if unknown := [key for key in row if key not in fields]:
                    raise ToolError(f"Row {number} has a field {unknown[0]!r} that is not in `fields`.")
                row = [row.get(name) for name in fields]
            if not isinstance(row, list) or len(row) != len(fields):
                raise ToolError(f"Row {number} must have exactly {len(fields)} values, one per field.")
            flat.extend(_cell(value) for value in row)

        not_editable = f"Table {table_key!r} cannot be edited. Check its key and import_type with list_tables"
        if table_version is None:
            table_version = (await api(TABLES + "GetTableForEditingArray", table_key, "", failure=not_editable))[0]
        await api(
            TABLES + "SetTableForEditingArray",
            table_key,
            table_version,
            fields,
            len(rows),
            flat,
            failure=f"CSiBridge rejected the new contents of {table_key!r}",
        )
        if not apply:
            return _render({"ok": True, "table": table_key, "rows": len(rows), "applied": False})

        result = await request("call", {"method": TABLES + "ApplyEditedTables", "args": [True]})
        if result.get("pending"):
            return _render(result)
        fatal, errors, warnings, infos, log = _outputs(result)
        return _render(
            {
                "ok": bool(result["ok"]) and fatal == 0 and errors == 0,
                "table": table_key,
                "rows": len(rows),
                "applied": True,
                "fatal_errors": fatal,
                "errors": errors,
                "warnings": warnings,
                "info_messages": infos,
                "import_log": log,
            }
        )

    return mcp
