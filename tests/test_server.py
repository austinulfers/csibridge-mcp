"""The MCP tools, exercised through a real MCP client session against the mock."""
import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from csibridge_mcp.server import create_server

pytestmark = pytest.mark.anyio

MODEL = r"C:\models\girder.bdb"


class ToolFailure(Exception):
    pass


@pytest.fixture
async def client(engine):
    async with create_connected_server_and_client_session(create_server(engine)) as session:
        yield session


async def tool(client, tool_name, /, **arguments):
    result = await client.call_tool(tool_name, arguments)
    text = result.content[0].text
    if result.isError:
        raise ToolFailure(text)
    return json.loads(text)


async def build_two_span_girder(client):
    """A two-span line girder: enough of a model to analyse."""
    assert (await tool(client, "new_model", units="kN_m_C"))["ok"] is True
    built = await tool(
        client,
        "batch",
        calls=[
            {"method": "SapModel.PropMaterial.SetMaterial", "args": ["CONC", "eMatType.Concrete"]},
            {"method": "SapModel.PropFrame.SetRectangle", "args": ["GIRDER", "CONC", 1.5, 0.6]},
            {"method": "SapModel.FrameObj.AddByCoord", "args": [0, 0, 0, 30, 0, 0], "kwargs": {"PropName": "GIRDER"}},
            {"method": "SapModel.FrameObj.AddByCoord", "args": [30, 0, 0, 60, 0, 0], "kwargs": {"PropName": "GIRDER"}},
        ],
    )
    assert built["failed"] == 0
    assert (await tool(client, "save_model", path=MODEL))["ok"] is True


# -- the tool surface -------------------------------------------------------


async def test_tools_are_listed_with_descriptions(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "status", "connect", "call", "batch", "api_search", "api_describe", "api_enum", "job_wait",
        "open_model", "save_model", "new_model", "run_analysis", "list_tables", "get_table", "edit_table",
    }  # fmt: skip
    assert all(t.description and len(t.description) > 30 for t in tools.values())
    assert tools["get_table"].annotations.readOnlyHint is True
    assert tools["new_model"].annotations.destructiveHint is True


async def test_status_flags_the_mock_backend(client):
    status = await tool(client, "status")
    assert status["connected"] is True
    assert "MOCK" in status["note"]


async def test_api_discovery_tools(client):
    found = await tool(client, "api_search", query="frame add")
    assert found["matches"][0].startswith("SapModel.FrameObj.AddBy")
    described = await tool(client, "api_describe", path="SapModel.FrameObj.AddByCoord")
    assert "Name: ref string" in described["signature"]
    assert (await tool(client, "api_describe", path="SapModel.FrameObj", filter="count"))["functions"] == ["Count() -> int"]
    assert (await tool(client, "api_enum", name="eUnits"))["members"]["kip_ft_F"] == 4


async def test_call_accepts_positional_or_named_arguments(client):
    added = await tool(client, "call", method="SapModel.FrameObj.AddByCoord", args=[0, 0, 0, 5, 0, 0])
    assert added["outputs"] == {"Name": "1"} and added["ok"] is True
    named = await tool(
        client,
        "call",
        method="SapModel.FrameObj.AddByPoint",
        kwargs={"Point1": "1", "Point2": "2", "UserName": "TIE"},
    )
    assert named["outputs"] == {"Name": "TIE"}
    as_object = await tool(client, "call", method="SapModel.FrameObj.GetPoints", args={"Name": "TIE"})
    assert as_object["outputs"] == {"Point1": "1", "Point2": "2"}


async def test_api_mistakes_come_back_as_tool_errors_with_guidance(client):
    with pytest.raises(ToolFailure, match="Did you mean: AddByCoord"):
        await tool(client, "call", method="SapModel.FrameObj.AddByCoords")
    with pytest.raises(ToolFailure, match="Unknown units 'furlongs'"):
        await tool(client, "new_model", units="furlongs")


# -- building, analysing, reading results -----------------------------------


async def test_build_analyse_and_read_results(client):
    await build_two_span_girder(client)
    assert (await tool(client, "status"))["csibridge"]["model_file"] == MODEL

    analysis = await tool(client, "run_analysis")
    assert analysis["ok"] is True
    assert (await tool(client, "status"))["csibridge"]["model_locked"] is True

    table = await tool(client, "get_table", table_key="Joint Displacements", where={"OutputCase": "DEAD"})
    assert table["fields"] == ["Joint", "OutputCase", "U1", "U2", "U3"]
    assert table["total_rows"] == 3
    assert [row[0] for row in table["rows"]] == ["1", "2", "3"]


async def test_analysis_failure_carries_a_hint(client):
    await tool(client, "new_model")
    result = await tool(client, "run_analysis")
    assert result["ok"] is False and "saved to a file" in result["hint"]


async def test_open_model(client):
    assert (await tool(client, "open_model", path=MODEL))["ok"] is True
    failed = await tool(client, "open_model", path=r"C:\models\notes.txt")
    assert failed["ok"] is False and "hint" in failed


# -- database tables --------------------------------------------------------


async def test_list_tables(client):
    await build_two_span_girder(client)
    listed = await tool(client, "list_tables")
    keys = [t["key"] for t in listed["tables"]]
    assert "Joint Coordinates" in keys and "Joint Displacements" not in keys  # no results yet

    with_empty = await tool(client, "list_tables", filter="joint", include_empty=True)
    assert {t["key"]: t.get("empty", False) for t in with_empty["tables"]} == {
        "Joint Coordinates": False,
        "Joint Displacements": True,
    }
    assert (await tool(client, "list_tables", limit=2))["returned"] == 2


async def test_get_table_fields_paging_and_errors(client):
    await build_two_span_girder(client)
    page = await tool(client, "get_table", table_key="Joint Coordinates", fields=["Joint", "XorR"], limit=2)
    assert page["fields"] == ["Joint", "XorR"]
    assert page["rows"] == [["1", "0"], ["2", "30"]]
    assert page["total_rows"] == 3 and "offset=2" in page["note"]
    rest = await tool(client, "get_table", table_key="Joint Coordinates", fields=["Joint", "XorR"], offset=2)
    assert rest["rows"] == [["3", "60"]] and "note" not in rest

    with pytest.raises(ToolFailure, match="Could not read table 'No Such Table'"):
        await tool(client, "get_table", table_key="No Such Table")
    with pytest.raises(ToolFailure, match="has no field 'Case'"):
        await tool(client, "get_table", table_key="Joint Coordinates", where={"Case": "DEAD"})


async def test_edit_table_round_trip(client):
    await build_two_span_girder(client)
    table = await tool(client, "get_table", table_key="Joint Coordinates", for_editing=True)
    rows = table["rows"]
    rows[1][table["fields"].index("XorR")] = 35  # move the pier; a number, not text, on purpose

    edited = await tool(
        client,
        "edit_table",
        table_key="Joint Coordinates",
        fields=table["fields"],
        rows=rows,
        table_version=table["table_version"],
    )
    assert edited["ok"] is True and edited["applied"] is True and edited["fatal_errors"] == 0
    moved = await tool(client, "call", method="SapModel.PointObj.GetCoordCartesian", args=["2"])
    assert moved["outputs"]["X"] == 35.0


async def test_edit_table_accepts_rows_as_objects_and_looks_up_the_version(client):
    await build_two_span_girder(client)
    fields = ["Joint", "CoordSys", "CoordType", "XorR", "Y", "Z"]
    rows = [
        {"Joint": "A", "CoordSys": "GLOBAL", "CoordType": "Cartesian", "XorR": 0, "Y": 0, "Z": 0},
        {"Joint": "B", "CoordSys": "GLOBAL", "CoordType": "Cartesian", "XorR": 12.5, "Y": 0, "Z": 0},
    ]
    edited = await tool(client, "edit_table", table_key="Joint Coordinates", fields=fields, rows=rows)
    assert edited["ok"] is True and edited["rows"] == 2
    names = await tool(client, "call", method="SapModel.PointObj.GetNameList")
    assert names["outputs"]["MyName"] == ["A", "B"]


async def test_edit_table_reports_csibridge_rejections(client):
    await build_two_span_girder(client)
    await tool(client, "run_analysis")  # locks the model
    table = await tool(client, "get_table", table_key="Joint Coordinates", for_editing=True)
    refused = await tool(client, "edit_table", table_key="Joint Coordinates", fields=table["fields"], rows=table["rows"])
    assert refused["ok"] is False and refused["fatal_errors"] == 1
    assert "locked" in refused["import_log"]

    with pytest.raises(ToolFailure, match="cannot be edited"):
        await tool(client, "edit_table", table_key="Joint Displacements", fields=["Joint"], rows=[["1"]])
    with pytest.raises(ToolFailure, match="Row 1 must have exactly 6 values"):
        await tool(client, "edit_table", table_key="Joint Coordinates", fields=table["fields"], rows=[["1", "GLOBAL"]])


async def test_staged_edits_are_not_applied(client):
    await build_two_span_girder(client)
    table = await tool(client, "get_table", table_key="Joint Coordinates", for_editing=True)
    staged = await tool(
        client, "edit_table", table_key="Joint Coordinates", fields=table["fields"], rows=table["rows"][:1], apply=False
    )
    assert staged["applied"] is False
    assert (await tool(client, "call", method="SapModel.PointObj.Count"))["ret"] == 3


# -- long-running operations ------------------------------------------------


async def test_long_analysis_returns_pending_then_job_wait_finishes_it(client, backend):
    await build_two_span_girder(client)
    backend.application.analysis_seconds = 0.4

    pending = await tool(client, "run_analysis", wait_seconds=0.05)
    assert pending["pending"] is True and "job_wait" in pending["note"]

    # Multi-step tools refuse to interleave with the running analysis.
    with pytest.raises(ToolFailure, match="CSiBridge is busy running SapModel.Analyze.RunAnalysis"):
        await tool(client, "get_table", table_key="Joint Coordinates")
    assert (await tool(client, "status"))["busy"]["job_id"] == pending["job_id"]

    finished = await tool(client, "job_wait", job_id=pending["job_id"], wait_seconds=5)
    assert finished["ok"] is True
    assert (await tool(client, "get_table", table_key="Joint Displacements"))["total_rows"] == 3


# -- the real transport -----------------------------------------------------


async def test_server_speaks_mcp_over_stdio():
    """Launch the server the way Claude Code does and talk to it over stdio."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "csibridge_mcp", "--mock"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "csibridge"
            assert "SapModel.BridgeModeler_1" in initialized.instructions
            assert len((await session.list_tools()).tools) == 15
            status = await tool(session, "status")
            assert status["backend"] == "mock" and status["connected"] is True
