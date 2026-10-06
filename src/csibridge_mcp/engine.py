"""The engine: owns the connection to CSiBridge and runs requests against it."""
from __future__ import annotations

import enum
import importlib
import json
import platform
import queue
import secrets
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .errors import CsiError
from .introspect import (
    ApiIndex,
    coerce_arguments,
    display_path,
    format_signature,
    shape_result,
    split_path,
)

DEFAULT_PROGID = "CSI.CSiBridge.API.SapObject"
DEFAULT_HELPER_PROGID = "CSiAPIv1.Helper"
DEFAULT_WAIT = 50.0
MAX_WAIT = 24 * 3600.0
MAX_BATCH_CALLS = 2000
MAX_KEPT_JOBS = 200

UNITS = {
    1: "lb_in_F", 2: "lb_ft_F", 3: "kip_in_F", 4: "kip_ft_F", 5: "kN_mm_C", 6: "kN_m_C",
    7: "kgf_mm_C", 8: "kgf_m_C", 9: "N_mm_C", 10: "N_m_C", 11: "Ton_mm_C", 12: "Ton_m_C",
    13: "kN_cm_C", 14: "kgf_cm_C", 15: "N_cm_C", 16: "Ton_cm_C",
}  # fmt: skip


def describe_exception(exc: BaseException) -> str:
    hresult = getattr(exc, "hresult", None)
    if isinstance(hresult, int):  # a COMError: (hresult, text, details)
        details = getattr(exc, "details", None)
        description = details[0] if isinstance(details, (tuple, list)) and details else None
        parts = [f"COM error 0x{hresult & 0xFFFFFFFF:08X}", getattr(exc, "text", None), description]
        return ": ".join(str(p).strip() for p in parts if p)
    return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Backends: the real COM API, and an in-memory mock
# ---------------------------------------------------------------------------


class ComBackend:
    """Talks to CSiBridge through its COM API using comtypes (Windows only)."""

    kind = "com"
    # RPC/COM failures that mean the CSiBridge process went away.
    _DEAD_HRESULTS = frozenset({0x800706BA, 0x800706BE, 0x800706BF, 0x80010108, 0x80010007, 0x80010012})

    def __init__(self, progid: str = DEFAULT_PROGID, helper_progid: str = DEFAULT_HELPER_PROGID):
        self.progid = progid
        self.helper_progid = helper_progid
        self._helper = None
        self._gen = None

    def thread_init(self) -> None:
        if sys.platform != "win32":
            raise CsiError(
                "unsupported_platform",
                "CSiBridge's API only exists on Windows, so this server must run on the Windows "
                "machine where CSiBridge is installed. For development elsewhere, start it with "
                "--mock to use a fake in-memory model.",
            )
        import comtypes
        import comtypes.client  # noqa: F401

        try:
            comtypes.CoInitialize()
        except OSError:
            pass  # this thread was already initialised for COM

    def _ensure_helper(self):
        if self._helper is not None:
            return self._helper
        import comtypes.client

        try:
            helper = comtypes.client.CreateObject(self.helper_progid)
        except Exception as exc:
            raise CsiError(
                "not_installed",
                f"Could not create the CSI API helper object {self.helper_progid!r} "
                f"({describe_exception(exc)}). Is CSiBridge installed on this machine? If it is, its "
                "API may need re-registering: see 'Setting API references' in CSI's API documentation.",
            ) from exc
        # CreateObject generated Python wrappers for the type library. The
        # module named after the library also carries the enum classes; the
        # raw wrapper module (found through the object) is the fallback.
        library = self.helper_progid.split(".")[0]
        try:
            self._gen = importlib.import_module(f"comtypes.gen.{library}")
        except ImportError:
            interface = getattr(helper, "__com_interface__", None)
            self._gen = sys.modules.get(getattr(interface, "__module__", None))
        helper_interface = getattr(self._gen, "cHelper", None)
        if helper_interface is not None:
            try:
                helper = helper.QueryInterface(helper_interface)
            except Exception:
                pass
        self._helper = helper
        return helper

    def _as_api_object(self, obj):
        root = getattr(self._gen, "cOAPI", None)
        if root is not None and getattr(obj, "__com_interface__", None) is not root:
            try:
                return obj.QueryInterface(root)
            except Exception:
                pass
        return obj

    def attach(self, pid: int | None = None):
        helper = self._ensure_helper()
        try:
            obj = helper.GetObjectProcess(self.progid, int(pid)) if pid else helper.GetObject(self.progid)
        except Exception as exc:
            raise CsiError(
                "not_running",
                f"Could not attach to a running CSiBridge ({describe_exception(exc)}). Start CSiBridge "
                "first; if it is running, make sure CSiBridge and your MCP client run as the same "
                "Windows user and at the same elevation (both normal, or both 'as administrator').",
            ) from exc
        if not obj:
            raise CsiError(
                "not_running",
                "No running CSiBridge instance was found. Start CSiBridge, or use connect with "
                "mode='launch' to start one.",
            )
        return self._as_api_object(obj)

    def launch(self, program_path: str | None = None):
        helper = self._ensure_helper()
        try:
            obj = helper.CreateObject(program_path) if program_path else helper.CreateObjectProgID(self.progid)
            obj = self._as_api_object(obj)
            status = obj.ApplicationStart()
        except Exception as exc:
            raise CsiError("launch_failed", f"Could not start CSiBridge ({describe_exception(exc)}).") from exc
        if isinstance(status, int) and status != 0:
            raise CsiError("launch_failed", f"CSiBridge ApplicationStart returned status {status}.")
        return obj

    def root_interface(self, api_object=None):
        interface = getattr(api_object, "__com_interface__", None) if api_object is not None else None
        if interface is None:
            self._ensure_helper()
            interface = getattr(self._gen, "cOAPI", None)
        return interface

    def enums(self) -> dict[str, dict[str, int]]:
        found: dict[str, dict[str, int]] = {}
        for name, obj in sorted(vars(self._gen).items()) if self._gen is not None else ():
            # comtypes generates each type-library enum as an IntFlag class.
            if isinstance(obj, type) and issubclass(obj, enum.Enum) and obj.__module__ != "enum":
                # __members__ rather than iteration: iterating a Flag skips
                # multi-bit values such as kip_in_F = 3.
                found[name] = {key: int(member.value) for key, member in obj.__members__.items()}
        return found

    def is_disconnect(self, exc: BaseException) -> bool:
        code = getattr(exc, "hresult", None)
        if not isinstance(code, int):
            code = getattr(exc, "winerror", None)
        return isinstance(code, int) and (code & 0xFFFFFFFF) in self._DEAD_HRESULTS


class MockBackend:
    """A fake in-memory model, for developing and testing without CSiBridge."""

    kind = "mock"

    def __init__(self):
        from . import mock_csi

        self._mock = mock_csi
        self.application = mock_csi.MockApplication()

    def thread_init(self) -> None:
        pass

    def attach(self, pid: int | None = None):
        if not self.application.running:
            raise CsiError(
                "not_running",
                "No running CSiBridge instance was found. Start CSiBridge, or use connect with "
                "mode='launch' to start one.",
            )
        return self.application.api_object

    def launch(self, program_path: str | None = None):
        self.application.running = True
        return self.application.api_object

    def root_interface(self, api_object=None):
        return self._mock.cOAPI

    def enums(self) -> dict[str, dict[str, int]]:
        return self._mock.ENUMS

    def is_disconnect(self, exc: BaseException) -> bool:
        return isinstance(exc, self._mock.MockDisconnected)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class _Job:
    op: str
    params: dict
    id: str = field(default_factory=lambda: secrets.token_hex(4))
    created: float = field(default_factory=time.time)
    started: float | None = None
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: CsiError | None = None

    @property
    def label(self) -> str:
        if self.op == "call":
            return str(self.params.get("method"))
        if self.op == "batch":
            calls = self.params.get("calls") or []
            first = calls[0].get("method") if calls and isinstance(calls[0], dict) else None
            return f"batch of {len(calls)} calls (first: {first})"
        return self.op


class Engine:
    """Runs operations against CSiBridge, one at a time, on a dedicated thread.

    COM objects belong to the thread that created them and the CSI API is not
    re-entrant, so every operation is queued to a single worker. Callers wait
    for a bounded time; a slow operation (an analysis run) comes back as a
    pending job that can be awaited again with the ``job`` operation.
    """

    def __init__(self, backend, allow_launch: bool = True):
        self.backend = backend
        self.allow_launch = allow_launch
        self._api_object = None
        self._index: ApiIndex | None = None
        self._index_error: str | None = None
        self._init_error: CsiError | None = None
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._jobs: dict[str, _Job] = {}
        self._jobs_lock = threading.Lock()
        self._current: _Job | None = None
        self._thread: threading.Thread | None = None
        self._ops = {
            "status": self._op_status,
            "connect": self._op_connect,
            "call": self._op_call,
            "batch": self._op_batch,
            "describe": self._op_describe,
            "search": self._op_search,
            "enums": self._op_enums,
        }

    # -- public, thread-safe entry points ---------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="csibridge-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=5)
            self._thread = None

    def request(self, op: str, params: dict | None = None, wait: float | None = None) -> dict:
        """Run one operation and return its result, or a pending-job notice.

        Raises CsiError for anything the caller should be told about.
        """
        params = params or {}
        wait = DEFAULT_WAIT if wait is None else max(0.0, min(float(wait), MAX_WAIT))
        if op == "job":
            return self._await(self._find_job(params.get("job_id")), wait)
        if op not in self._ops:
            raise CsiError("bad_request", f"Unknown operation {op!r}.")
        if op == "status" and (busy := self.busy()) is not None:
            # Answer without queueing behind whatever is running.
            return {**self._runtime_info(), "connected": self._api_object is not None, "busy": busy}
        job = _Job(op, params)
        with self._jobs_lock:
            self._jobs[job.id] = job
            for old in list(self._jobs.values())[: max(0, len(self._jobs) - MAX_KEPT_JOBS)]:
                if old.done.is_set():
                    del self._jobs[old.id]
        self._queue.put(job)
        return self._await(job, wait)

    def busy(self) -> dict | None:
        """What the worker is running right now, or None if it is idle."""
        job = self._current
        if job is None:
            return None
        return {
            "job_id": job.id,
            "label": job.label,
            "running_seconds": round(time.time() - (job.started or job.created), 1),
            "queued_behind": self._queue.qsize(),
        }

    # -- job plumbing -----------------------------------------------------

    def _find_job(self, job_id: Any) -> _Job:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise CsiError("not_found", f"Unknown job id {job_id!r} (results of old jobs are discarded).")
        return job

    def _await(self, job: _Job, wait: float) -> dict:
        if not job.done.wait(wait):
            return {
                "pending": True,
                "job_id": job.id,
                "label": job.label,
                "state": "running" if job.started else "queued",
                "elapsed_seconds": round(time.time() - (job.started or job.created), 1),
            }
        if job.error is not None:
            raise job.error
        return job.result

    def _run(self) -> None:
        try:
            self.backend.thread_init()
        except CsiError as exc:
            self._init_error = exc
        except Exception as exc:
            self._init_error = CsiError(
                "not_installed", f"Could not initialise the CSiBridge backend ({describe_exception(exc)})."
            )
        while (job := self._queue.get()) is not None:
            self._current = job
            job.started = time.time()
            try:
                if self._init_error is not None:
                    raise self._init_error
                job.result = self._ops[job.op](job.params)
            except CsiError as exc:
                job.error = exc
            except Exception as exc:  # a bug here: report it rather than killing the worker
                job.error = CsiError("internal", describe_exception(exc), detail=traceback.format_exc())
            finally:
                self._current = None
                job.done.set()
        self._api_object = None

    # -- connection (worker thread only) ----------------------------------

    def _runtime_info(self) -> dict:
        return {
            "server_version": __version__,
            "backend": self.backend.kind,
            "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "python": f"{platform.python_version()} {platform.architecture()[0]}",
        }

    def _attach(self, pid: int | None = None) -> None:
        self._api_object = self.backend.attach(pid)
        self._index_error = None

    def _ensure_connected(self) -> None:
        if self._api_object is None:
            self._attach()

    def _get_index(self) -> ApiIndex | None:
        if self._index is None and self._index_error is None:
            try:
                root = self.backend.root_interface(self._api_object)
                if root is None:
                    raise RuntimeError("the API type library exposes no root interface")
                self._index = ApiIndex(root, self.backend.enums())
            except CsiError as exc:
                self._index_error = exc.message
            except Exception as exc:
                self._index_error = describe_exception(exc)
        return self._index

    def _require_index(self) -> ApiIndex:
        index = self._get_index()
        if index is None:
            raise CsiError(
                "unavailable",
                f"API type information is not available ({self._index_error}). "
                "Functions can still be called by path.",
            )
        return index

    # -- calling the API (worker thread only) -----------------------------

    def _invoke(self, segments: list[str], args: list, kwargs: dict) -> dict:
        shown = display_path(segments)
        index = self._get_index()
        member = None
        if index is not None:
            if index.interface_at(segments) is not None:
                raise CsiError(
                    "bad_request",
                    f"{shown} is a group of functions, not a function. Use api_describe to list them.",
                )
            member = index.member(segments)
            if member is None:
                raise CsiError("not_found", index.not_found_message(segments))
            args, kwargs = coerce_arguments(shown, member, args, kwargs, index)
        started = time.time()
        try:
            target = self._api_object
            for segment in segments[1:]:
                target = getattr(target, segment)
            raw = target(*args, **kwargs) if callable(target) else target
        except Exception as exc:
            raise self._translate_call_error(exc, shown, member) from exc
        return shape_result(shown, member, raw, time.time() - started)

    def _translate_call_error(self, exc: Exception, shown: str, member: dict | None) -> CsiError:
        if self.backend.is_disconnect(exc):
            self._api_object = None
            return CsiError(
                "connection_lost",
                f"Lost the connection to CSiBridge while calling {shown} (was CSiBridge closed?). "
                f"Start it again and reconnect. [{describe_exception(exc)}]",
            )
        signature = f" Signature: {format_signature(shown, member)}" if member is not None else ""
        if isinstance(exc, AttributeError) and member is None:
            return CsiError("not_found", f"No such API path {shown} ({exc}).")
        if isinstance(exc, (TypeError, ValueError)) or type(exc).__name__ == "ArgumentError":
            return CsiError("bad_request", f"Bad arguments for {shown}: {exc}.{signature}")
        return CsiError("api_error", f"{shown} raised {describe_exception(exc)}.{signature}")

    def _call_one(self, spec: Any) -> dict:
        if not isinstance(spec, dict):
            raise CsiError("bad_request", "Each call must be an object with a 'method'.")
        args = spec.get("args")
        kwargs = spec.get("kwargs") or {}
        if isinstance(args, dict):  # named arguments given in place of a list
            args, kwargs = [], {**args, **kwargs}
        if not isinstance(kwargs, dict):
            raise CsiError("bad_request", "kwargs must be an object of name: value.")
        return self._invoke(split_path(spec.get("method")), list(args or []), kwargs)

    # -- operations (worker thread only) ----------------------------------

    def _op_status(self, params: dict) -> dict:
        info = self._runtime_info()
        connect_error = None
        if self._api_object is None and params.get("auto_connect", True):
            try:
                self._attach()
            except CsiError as exc:
                connect_error = exc.message
        if self._api_object is not None:
            try:
                info["csibridge"] = self._model_info()
            except CsiError as exc:
                connect_error = exc.message
        info["connected"] = self._api_object is not None
        if connect_error:
            info["connect_error"] = connect_error
        index = self._get_index()
        info["api_index"] = index.summary() if index else {"ready": False, "error": self._index_error}
        return info

    def _model_info(self) -> dict:
        def grab(method: str, *args) -> dict | None:
            try:
                return self._invoke(split_path(method), list(args), {})
            except CsiError as exc:
                if exc.kind == "connection_lost":
                    raise
                return None

        def outputs(result: dict | None) -> list:
            if result is None:
                return []
            if "outputs" in result:
                return list(result["outputs"].values())
            return list(result.get("raw") or [])[:-1]

        info: dict[str, Any] = {}
        program = outputs(grab("SapModel.GetProgramInfo"))
        if len(program) >= 3:
            info["program"], info["program_version"], info["program_level"] = program[:3]
        elif version := outputs(grab("SapModel.GetVersion")):
            info["program_version"] = version[0]
        if (api_version := grab("SapObject.GetOAPIVersionNumber")) is not None:
            info["api_version"] = api_version.get("ret")
        if (filename := grab("SapModel.GetModelFilename", True)) is not None:
            info["model_file"] = filename.get("ret") or None
        units = grab("SapModel.GetPresentUnits")
        if units is not None and isinstance(units.get("ret"), int):
            info["units"] = {"code": units["ret"], "name": UNITS.get(units["ret"])}
        if (locked := grab("SapModel.GetModelIsLocked")) is not None:
            info["model_locked"] = locked.get("ret")
        return info

    def _op_connect(self, params: dict) -> dict:
        mode = params.get("mode", "attach")
        if mode == "attach":
            self._api_object = None
            self._attach(params.get("pid"))
        elif mode == "launch":
            if not self.allow_launch:
                raise CsiError("forbidden", "This server was started with --no-launch.")
            self._api_object = None
            self._api_object = self.backend.launch(params.get("program_path"))
            self._index_error = None
        else:
            raise CsiError("bad_request", "mode must be 'attach' or 'launch'.")
        return self._op_status({"auto_connect": False})

    def _op_call(self, params: dict) -> dict:
        self._ensure_connected()
        return self._call_one(params)

    def _op_batch(self, params: dict) -> dict:
        calls = params.get("calls")
        if not isinstance(calls, list) or not calls:
            raise CsiError("bad_request", "calls must be a non-empty list.")
        if len(calls) > MAX_BATCH_CALLS:
            raise CsiError("bad_request", f"A batch may contain at most {MAX_BATCH_CALLS} calls.")
        stop_on_error = bool(params.get("stop_on_error", True))
        self._ensure_connected()
        results = []
        failed = 0
        for spec in calls:
            lost = False
            try:
                result = self._call_one(spec)
            except CsiError as exc:
                lost = exc.kind == "connection_lost"
                method = spec.get("method") if isinstance(spec, dict) else None
                result = {"method": method, "ok": False, "error": exc.message}
            results.append(result)
            if not result["ok"]:
                failed += 1
                if stop_on_error or lost:
                    break
        return {
            "total": len(calls),
            "completed": len(results),
            "failed": failed,
            "stopped_early": len(results) < len(calls),
            "results": results,
        }

    def _op_describe(self, params: dict) -> dict:
        return self._require_index().describe(params.get("path"), params.get("filter"))

    def _op_search(self, params: dict) -> dict:
        return self._require_index().search(params.get("query"), params.get("limit") or 30)

    def _op_enums(self, params: dict) -> dict:
        return self._require_index().describe_enum(params.get("name"))


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def selftest(engine: Engine, launch: bool = False) -> int:
    """Attach to CSiBridge and run read-only checks, printing a report.

    This exists because the COM path can only be exercised on a Windows machine
    with CSiBridge installed: its output shows exactly which layer misbehaves.
    """
    failures = []

    def step(title: str, op: str, params: dict | None = None, check=None) -> None:
        print(f"\n== {title}")
        try:
            result = engine.request(op, params or {}, wait=600)
            print(json.dumps(result, indent=2)[:4000])
            problem = check(result) if check else None
        except CsiError as exc:
            problem = f"{exc.kind}: {exc.message}"
            if exc.detail:
                print(exc.detail)
        if problem:
            failures.append((title, problem))
            print(f"FAIL: {problem}")
        else:
            print("PASS")

    print(f"csibridge-mcp self-test (version {__version__}, backend {engine.backend.kind})")
    print(f"Python {platform.python_version()} {platform.architecture()[0]} on {platform.platform()}")
    if launch:
        step("Launch CSiBridge", "connect", {"mode": "launch"})
    step(
        "Attach and read model info",
        "status",
        check=lambda r: None if r.get("connected") else r.get("connect_error", "not connected"),
    )
    step(
        "Read the API type library",
        "describe",
        {"path": "SapModel"},
        check=lambda r: None if r.get("functions") else "no functions found on SapModel",
    )
    step("Signature of a function with ref parameters", "describe", {"path": "SapModel.FrameObj.AddByCoord"})
    step("Look for the Bridge Modeler API", "search", {"query": "BridgeModeler", "limit": 5})
    step("Call a value-returning function", "call", {"method": "SapModel.GetPresentUnits"})
    step(
        "Call a function with ref outputs",
        "call",
        {"method": "SapModel.LoadPatterns.GetNameList"},
        check=lambda r: None if r.get("ok") and "outputs" in r else "outputs were not labelled",
    )
    step(
        "List available database tables",
        "call",
        {"method": "SapModel.DatabaseTables.GetAvailableTables"},
        check=lambda r: None if r.get("ok") else r.get("error"),
    )

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for title, problem in failures:
            print(f"  - {title}: {problem}")
        return 1
    print("All checks passed.")
    return 0
