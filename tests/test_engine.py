import sys
import time

import pytest

from csibridge_mcp.engine import ComBackend, Engine
from csibridge_mcp.errors import CsiError


def call(engine, method, *args, **kwargs):
    return engine.request("call", {"method": method, "args": list(args), "kwargs": kwargs})


def wait_until_idle(engine):
    deadline = time.time() + 5
    while engine.busy() is not None and time.time() < deadline:
        time.sleep(0.01)


# -- connecting -------------------------------------------------------------


def test_status_attaches_and_describes_the_model(engine):
    status = engine.request("status")
    assert status["connected"] is True
    assert status["backend"] == "mock"
    assert status["csibridge"]["units"] == {"code": 6, "name": "kN_m_C"}
    assert status["csibridge"]["model_locked"] is False
    assert status["api_index"]["ready"] is True


def test_status_explains_when_csibridge_is_not_running(engine, backend):
    backend.application.running = False
    status = engine.request("status")
    assert status["connected"] is False
    assert "No running CSiBridge" in status["connect_error"]
    # The API can still be explored without a running instance.
    assert engine.request("search", {"query": "frame"})["total_matches"] > 0


def test_calls_fail_clearly_until_csibridge_is_launched(engine, backend):
    backend.application.running = False
    with pytest.raises(CsiError) as error:
        call(engine, "SapModel.GetPresentUnits")
    assert error.value.kind == "not_running"

    assert engine.request("connect", {"mode": "launch"})["connected"] is True
    assert call(engine, "SapModel.GetPresentUnits")["ret"] == 6


def test_launch_can_be_forbidden(backend):
    engine = Engine(backend, allow_launch=False)
    engine.start()
    try:
        with pytest.raises(CsiError) as error:
            engine.request("connect", {"mode": "launch"})
        assert error.value.kind == "forbidden"
    finally:
        engine.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="checks the message shown off Windows")
def test_real_backend_off_windows_says_what_to_do():
    engine = Engine(ComBackend())
    engine.start()
    try:
        with pytest.raises(CsiError) as error:
            engine.request("status")
        assert error.value.kind == "unsupported_platform"
        assert "--mock" in error.value.message
    finally:
        engine.stop()


# -- calling ----------------------------------------------------------------


def test_call_with_positional_arguments_and_omitted_refs(engine):
    result = call(engine, "SapModel.FrameObj.AddByCoord", 0, 0, 0, 10, 0, 0)
    assert result == {"method": "SapModel.FrameObj.AddByCoord", "ret": 0, "outputs": {"Name": "1"}, "ok": True}
    points = call(engine, "SapModel.FrameObj.GetPoints", "1")
    assert points["outputs"] == {"Point1": "1", "Point2": "2"}


def test_call_with_named_arguments(engine):
    result = engine.request(
        "call",
        {"method": "SapModel.FrameObj.AddByCoord", "args": {"xi": 0, "yi": 0, "zi": 0, "xj": 0, "yj": 0, "zj": 3, "UserName": "COL"}},
    )
    assert result["outputs"] == {"Name": "COL"}


def test_json_numbers_and_enum_names_are_accepted(engine):
    # JSON clients often send 4.0 for an int; ctypes alone would reject it.
    assert call(engine, "SapModel.SetPresentUnits", 4.0)["ok"] is True
    assert call(engine, "SapModel.GetPresentUnits")["ret"] == 4
    assert call(engine, "SapModel.SetPresentUnits", "eUnits.N_mm_C")["ok"] is True
    assert call(engine, "SapModel.GetPresentUnits")["ret"] == 9


def test_arrays_round_trip(engine):
    call(engine, "SapModel.PointObj.AddCartesian", 0, 0, 0, UserName="P")
    fixed = [True, True, True, False, False, False]
    assert call(engine, "SapModel.PointObj.SetRestraint", "P", fixed)["ok"] is True
    assert call(engine, "SapModel.PointObj.GetRestraint", "P")["outputs"]["Value"] == fixed
    names = call(engine, "SapModel.PointObj.GetNameList")
    assert names["outputs"] == {"NumberNames": 1, "MyName": ["P"]}


def test_nonzero_status_is_a_failure_but_nonzero_values_are_not(engine):
    failed = call(engine, "SapModel.FrameObj.Delete", "missing")
    assert failed["ok"] is False and failed["ret"] == 1

    call(engine, "SapModel.FrameObj.AddByCoord", 0, 0, 0, 1, 0, 0)
    count = call(engine, "SapModel.FrameObj.Count")
    assert count == {"method": "SapModel.FrameObj.Count", "ret": 1, "ok": True}


@pytest.mark.parametrize(
    ("method", "args", "kind", "fragment"),
    [
        ("SapModel.FrameObj.AddByCoords", [], "not_found", "Did you mean: AddByCoord"),
        ("SapModel.FrameObj", [], "bad_request", "group of functions"),
        ("SapModel.SetPresentUnits", ["metric"], "bad_request", "Signature: SapModel.SetPresentUnits(Units: int)"),
        ("SapModel.SetPresentUnits", [], "bad_request", "missing required argument(s): Units"),
        ("SapModel.FrameObj.Release", [], "bad_request", "Invalid API path"),
    ],
)
def test_bad_calls_are_explained(engine, method, args, kind, fragment):
    with pytest.raises(CsiError) as error:
        call(engine, method, *args)
    assert error.value.kind == kind
    assert fragment in error.value.message


def test_batch_stops_at_the_first_failure_by_default(engine):
    calls = [
        {"method": "SapModel.PropMaterial.SetMaterial", "args": ["CONC", 2]},
        {"method": "SapModel.PropFrame.SetRectangle", "args": ["R1", "NO_SUCH_MATERIAL", 0.5, 0.3]},
        {"method": "SapModel.PropFrame.SetRectangle", "args": ["R2", "CONC", 0.5, 0.3]},
    ]
    stopped = engine.request("batch", {"calls": calls})
    assert (stopped["completed"], stopped["failed"], stopped["stopped_early"]) == (2, 1, True)

    continued = engine.request("batch", {"calls": calls, "stop_on_error": False})
    assert (continued["completed"], continued["failed"], continued["stopped_early"]) == (3, 1, False)
    assert [r["ok"] for r in continued["results"]] == [True, False, True]


def test_batch_reports_malformed_calls_without_aborting(engine):
    calls = [{"method": "SapModel.Nope.Nothing"}, {"method": "SapModel.FrameObj.Count"}]
    result = engine.request("batch", {"calls": calls, "stop_on_error": False})
    assert result["results"][0]["ok"] is False and "No such API path" in result["results"][0]["error"]
    assert result["results"][1]["ret"] == 0


# -- losing the connection --------------------------------------------------


def test_closing_csibridge_is_detected_and_recoverable(engine):
    assert call(engine, "SapObject.ApplicationExit", False)["ok"] is True
    with pytest.raises(CsiError) as error:
        call(engine, "SapModel.GetPresentUnits")
    assert error.value.kind == "connection_lost"

    assert engine.request("status")["connected"] is False
    assert engine.request("connect", {"mode": "launch"})["connected"] is True
    assert call(engine, "SapModel.GetPresentUnits")["ok"] is True


# -- long-running operations ------------------------------------------------


def test_slow_calls_become_pending_jobs(engine, backend):
    backend.application.analysis_seconds = 0.4
    call(engine, "SapModel.FrameObj.AddByCoord", 0, 0, 0, 10, 0, 0)
    call(engine, "SapModel.File.Save", r"C:\models\slow.bdb")

    pending = engine.request("call", {"method": "SapModel.Analyze.RunAnalysis"}, wait=0.05)
    assert pending["pending"] is True and pending["state"] == "running"

    # Status answers immediately instead of queueing behind the analysis.
    status = engine.request("status", wait=0.05)
    assert status["busy"]["label"] == "SapModel.Analyze.RunAnalysis"

    # A call made meanwhile queues, and can itself be awaited later.
    queued = engine.request("call", {"method": "SapModel.GetModelIsLocked"}, wait=0.01)
    assert queued["pending"] is True and queued["state"] == "queued"

    finished = engine.request("job", {"job_id": pending["job_id"]}, wait=5)
    assert finished["ok"] is True and finished["ret"] == 0
    assert engine.request("job", {"job_id": queued["job_id"]}, wait=5)["ret"] is True


def test_unknown_job_and_operation(engine):
    with pytest.raises(CsiError) as error:
        engine.request("job", {"job_id": "nope"})
    assert error.value.kind == "not_found"
    with pytest.raises(CsiError) as error:
        engine.request("explode")
    assert error.value.kind == "bad_request"


def test_a_bug_in_an_operation_does_not_kill_the_worker(engine, monkeypatch):
    def broken(params):
        raise RuntimeError("boom")

    monkeypatch.setitem(engine._ops, "search", broken)
    with pytest.raises(CsiError) as error:
        engine.request("search", {"query": "frame"})
    assert error.value.kind == "internal" and "boom" in error.value.message
    assert call(engine, "SapModel.GetPresentUnits")["ok"] is True
