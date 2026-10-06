"""Reading the CSiBridge API's shape from its COM type library.

comtypes generates one Python class per COM interface, and each class carries a
``_methods_`` list describing every function: parameter names, ctypes types and
in/out flags. Reading that gives the exact signatures of whichever CSiBridge
version is installed, so nothing here depends on a copy of CSI's documentation.

Everything in this module is duck-typed against those generated classes (it
never imports comtypes), which is what lets the mock in ``mock_csi`` stand in
for them on machines without Windows.
"""
from __future__ import annotations

import difflib
import enum
import json
import math
import re
from typing import Any

from .errors import CsiError

ROOT = "SapObject"

_SEGMENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# COM/ctypes plumbing that must never be reachable through a call path.
_FORBIDDEN_SEGMENTS = frozenset({
    "QueryInterface", "AddRef", "Release", "GetTypeInfo", "GetTypeInfoCount", "GetIDsOfNames",
    "Invoke", "value", "contents", "from_param", "from_address", "from_buffer",
    "from_buffer_copy", "in_dll",
})  # fmt: skip

# PARAMFLAG_* bits as stored in comtypes paramflags.
_PF_IN, _PF_OUT, _PF_RETVAL, _PF_OPT = 1, 2, 8, 16
_BASE_INTERFACES = frozenset({"object", "IUnknown", "IDispatch"})
_MAX_TREE_DEPTH = 8

_SIMPLE_TYPES = {
    "c_double": "double", "c_float": "double",
    "c_int": "int", "c_long": "int", "c_short": "int", "c_longlong": "int",
    "c_uint": "int", "c_ulong": "int", "c_ushort": "int", "c_ulonglong": "int",
    "c_byte": "int", "c_ubyte": "int",
    "BSTR": "string", "c_wchar_p": "string", "c_char_p": "string",
    "VARIANT_BOOL": "bool", "c_bool": "bool",
    "VARIANT": "variant", "HRESULT": "hresult",
}  # fmt: skip


def to_jsonable(value: Any) -> Any:
    """Convert a value returned by the COM layer into plain JSON data."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, enum.Enum):
        return to_jsonable(value.value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    inner = getattr(value, "value", None)  # ctypes simple instances
    if isinstance(inner, (bool, int, float, str)):
        return to_jsonable(inner)
    return f"<{type(value).__name__}>"


# ---------------------------------------------------------------------------
# API paths
# ---------------------------------------------------------------------------


def split_path(path: Any) -> list[str]:
    """Turn 'SapModel.FrameObj.GetNameList' into validated segments.

    Paths are rooted at ``SapObject``. ``SapModel.X`` is shorthand for
    ``SapObject.SapModel.X``, and a path with no known root is assumed to hang
    off ``SapModel``.
    """
    if not isinstance(path, str) or not path.strip():
        raise CsiError("bad_request", "Expected a dotted API path such as 'SapModel.FrameObj.GetNameList'.")
    text = path.strip().removesuffix("()")
    segments = text.split(".")
    if segments[0] == "SapModel":
        segments.insert(0, ROOT)
    elif segments[0] != ROOT:
        segments = [ROOT, "SapModel", *segments]
    for segment in segments:
        if not _SEGMENT.match(segment) or segment in _FORBIDDEN_SEGMENTS:
            raise CsiError("bad_request", f"Invalid API path {path!r}.")
    return segments


def display_path(segments: list[str]) -> str:
    """Render segments the way CSI's documentation writes them."""
    if segments[:2] == [ROOT, "SapModel"]:
        return ".".join(segments[1:])
    return ".".join(segments)


# ---------------------------------------------------------------------------
# Describing members
# ---------------------------------------------------------------------------


def _pointee(ctype: Any) -> Any:
    """The type a ctypes pointer type points at, or None if it is not a pointer."""
    inner = getattr(ctype, "_type_", None)  # simple types keep a str code here
    return inner if isinstance(inner, type) else None


def type_name(ctype: Any) -> str:
    """Friendly name for a ctypes/comtypes type: double, string[], cFrameObj..."""
    if ctype is None:
        return "void"
    item = getattr(ctype, "_itemtype_", None)  # pointer to a SAFEARRAY
    if item is not None:
        return type_name(item) + "[]"
    interface = getattr(ctype, "__com_interface__", None)
    if interface is not None:
        return interface.__name__
    name = getattr(ctype, "__name__", None) or str(ctype)
    if name in _SIMPLE_TYPES:
        return _SIMPLE_TYPES[name]
    inner = _pointee(ctype)
    return type_name(inner) if inner is not None else name


def _plain_default(value: Any) -> Any:
    """A declared default as a JSON scalar, or None if it has no simple form."""
    if hasattr(value, "value"):
        value = value.value
    return value if isinstance(value, (bool, int, float, str)) else None


def _member_from_com_spec(spec: Any) -> tuple[dict | None, Any]:
    """Describe one entry of a comtypes ``_methods_`` list.

    Returns ``(member, child_interface_class)``, or ``(None, None)`` for
    entries that cannot be called from here (property setters).
    """
    name, argtypes, paramflags, idlflags = spec[1], spec[2] or (), spec[3] or (), spec[4] or ()
    kind = "method"
    if name.startswith("_get_") or "propget" in idlflags:
        kind = "property"
        name = name.removeprefix("_get_")
    elif name.startswith(("_set_", "_setref_")) or "propput" in idlflags or "propputref" in idlflags:
        return None, None

    params: list[dict] = []
    returns = "void"
    child = None
    for position, ctype in enumerate(argtypes):
        flag_info = paramflags[position] if position < len(paramflags) else (_PF_IN, None)
        flags = flag_info[0] or _PF_IN
        param_name = flag_info[1] or f"arg{position + 1}"
        if flags & _PF_RETVAL:
            target = _pointee(ctype) or ctype
            returns = type_name(target)
            child = getattr(target, "__com_interface__", None)
            continue
        is_out = bool(flags & _PF_OUT)
        is_in = bool(flags & _PF_IN) or not is_out
        param = {
            "name": param_name,
            "type": type_name((_pointee(ctype) or ctype) if is_out else ctype),
            "dir": "ref" if (is_in and is_out) else ("out" if is_out else "in"),
        }
        if len(flag_info) > 2 or flags & _PF_OPT:
            param["optional"] = True
            if len(flag_info) > 2 and (default := _plain_default(flag_info[2])) is not None:
                param["default"] = default
        params.append(param)
    return {"name": name, "kind": kind, "params": params, "returns": returns}, child


def _member_from_disp_spec(spec: Any) -> tuple[dict | None, Any]:
    """Describe one entry of a comtypes ``_disp_methods_`` list (best effort)."""
    what, name, idlflags, restype, argspec = spec[0], spec[1], spec[2] or (), spec[3], spec[4] or ()
    if "propput" in idlflags or "propputref" in idlflags:
        return None, None
    params = []
    for position, item in enumerate(argspec):
        idl, ctype = item[0], item[1]
        is_out = "out" in idl
        param = {
            "name": (item[2] if len(item) > 2 else None) or f"arg{position + 1}",
            "type": type_name((_pointee(ctype) or ctype) if is_out else ctype),
            "dir": "ref" if (is_out and "in" in idl) else ("out" if is_out else "in"),
        }
        if "optional" in idl or len(item) > 3:
            param["optional"] = True
        params.append(param)
    kind = "property" if (what == "DISPPROPERTY" or "propget" in idlflags) else "method"
    member = {"name": name, "kind": kind, "params": params, "returns": type_name(restype)}
    return member, getattr(restype, "__com_interface__", None)


def format_signature(path: str, member: dict) -> str:
    """One-line signature: ``SapModel.X.Fn(a: double, b: ref string) -> int``."""
    if member["kind"] == "property":
        return f"{path} -> {member['returns']}  (property)"
    parts = []
    for param in member["params"]:
        prefix = {"ref": "ref ", "out": "out "}.get(param["dir"], "")
        text = f"{param['name']}: {prefix}{param['type']}"
        if "default" in param:
            text += f" = {json.dumps(param['default'])}"
        elif param.get("optional"):
            text += " = <default>"
        parts.append(text)
    return f"{path}({', '.join(parts)}) -> {member['returns']}"


def returns_status(member: dict) -> bool:
    """Whether a function's int return value is a status code (0 = success).

    Nearly every CSI function returns a status code, but a few return a value
    instead (``Count()``, ``GetPresentUnits()``); those must not be mistaken
    for failures when they return nonzero.
    """
    if member["kind"] != "method" or member["returns"] != "int":
        return False
    name = member["name"]
    if name.startswith("Count"):
        return False
    has_outputs = any(p["dir"] != "in" for p in member["params"])
    return has_outputs or not name.startswith("Get")


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


class ApiIndex:
    """Every interface and function reachable from the root API object."""

    def __init__(self, root_interface: type, enums: dict[str, dict[str, int]] | None = None):
        self.interfaces: dict[str, dict[str, dict]] = {}  # interface name -> {member name: member}
        self.paths: dict[str, str] = {}  # dotted path from SapObject -> interface name
        self.enums = enums or {}
        self._children: dict[str, dict[str, type]] = {}  # interface name -> {property: interface class}
        self._walk(root_interface)

    def _describe_interface(self, interface: type) -> tuple[dict, dict]:
        members: dict[str, dict] = {}
        children: dict[str, type] = {}
        for klass in reversed(interface.__mro__):
            if klass.__name__ in _BASE_INTERFACES:
                continue
            described = [_member_from_com_spec(s) for s in klass.__dict__.get("_methods_") or ()]
            described += [_member_from_disp_spec(s) for s in klass.__dict__.get("_disp_methods_") or ()]
            for member, child in described:
                if member is None:
                    continue
                members[member["name"]] = member
                if child is not None:
                    children[member["name"]] = child
        return members, children

    def _walk(self, root_interface: type) -> None:
        pending = [(ROOT, root_interface, (root_interface.__name__,))]
        while pending:
            path, interface, chain = pending.pop(0)
            name = interface.__name__
            self.paths[path] = name
            if name not in self.interfaces:
                self.interfaces[name], self._children[name] = self._describe_interface(interface)
            for prop, child in self._children[name].items():
                if child.__name__ in chain or len(chain) >= _MAX_TREE_DEPTH:
                    continue  # guard against interfaces that refer back to themselves
                pending.append((f"{path}.{prop}", child, (*chain, child.__name__)))

    # -- lookups ----------------------------------------------------------

    def interface_at(self, segments: list[str]) -> str | None:
        return self.paths.get(".".join(segments))

    def member(self, segments: list[str]) -> dict | None:
        interface = self.paths.get(".".join(segments[:-1]))
        return self.interfaces[interface].get(segments[-1]) if interface else None

    def _functions(self, interface: str) -> list[dict]:
        children = self._children[interface]
        return [m for m in self.interfaces[interface].values() if m["name"] not in children]

    def not_found_message(self, segments: list[str]) -> str:
        parent_segments, leaf = segments[:-1], segments[-1]
        parent = self.paths.get(".".join(parent_segments))
        if parent is not None:
            shown = display_path(parent_segments)
            close = difflib.get_close_matches(leaf, list(self.interfaces[parent]), n=5, cutoff=0.5)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            return f"{shown} has no function {leaf!r}.{hint} Use api_describe on {shown} to list its functions."
        elsewhere = [
            display_path([*path.split("."), leaf])
            for path, interface in self.paths.items()
            if leaf in self.interfaces[interface]
        ]
        if elsewhere:
            return (
                f"No such API path {display_path(segments)}. "
                f"A function with that name exists at: {', '.join(elsewhere[:5])}."
            )
        return f"No such API path {display_path(segments)}. Use api_search to find the right function."

    # -- queries ----------------------------------------------------------

    def summary(self) -> dict:
        functions = sum(len(self._functions(name)) for name in self.interfaces)
        return {"ready": True, "interfaces": len(self.interfaces), "functions": functions, "enums": len(self.enums)}

    def describe(self, path: str | None = None, name_filter: str | None = None) -> dict:
        """Describe the whole tree, one interface, or one function."""
        if not path:
            return {
                "interfaces": [
                    f"{display_path(dotted.split('.'))}  ({len(self._functions(interface))} functions)"
                    for dotted, interface in self.paths.items()
                ]
            }
        segments = split_path(path)
        shown = display_path(segments)
        interface = self.interface_at(segments)
        if interface is not None:
            needle = (name_filter or "").lower()
            result = {
                "path": shown,
                "interface": interface,
                "functions": [
                    format_signature(m["name"], m)
                    for m in self._functions(interface)
                    if needle in m["name"].lower()
                ],
            }
            if self._children[interface]:
                result["sub_interfaces"] = [f"{shown}.{prop}" for prop in self._children[interface]]
            return result

        member = self.member(segments)
        if member is None:
            raise CsiError("not_found", self.not_found_message(segments))
        result: dict[str, Any] = {"path": shown, "signature": format_signature(shown, member)}
        if outputs := [p["name"] for p in member["params"] if p["dir"] != "in"]:
            result["outputs"] = outputs
        result["return_is_status_code"] = returns_status(member)
        return result

    def search(self, query: str, limit: int = 30) -> dict:
        """Find functions whose path or parameter names contain every word of the query."""
        tokens = [t for t in re.split(r"[\s.,()]+", (query or "").lower()) if t]
        if not tokens:
            raise CsiError("bad_request", "Search query is empty.")
        scored = []
        for dotted, interface in self.paths.items():
            shown = display_path(dotted.split("."))
            for member in self._functions(interface):
                name = member["name"].lower()
                full = f"{shown}.{member['name']}".lower()
                params = " ".join(p["name"] for p in member["params"]).lower()
                score = 0
                for token in tokens:
                    if token == name:
                        score += 6
                    elif token in name:
                        score += 4
                    elif token in full:
                        score += 2
                    elif token in params:
                        score += 1
                    else:
                        score = 0
                        break
                if score:
                    scored.append((-score, len(scored), format_signature(f"{shown}.{member['name']}", member)))
        scored.sort()
        result = {
            "query": query,
            "total_matches": len(scored),
            "matches": [text for _, _, text in scored[: max(1, int(limit))]],
        }
        enum_hits = [
            enum_name
            for enum_name, members in self.enums.items()
            if all(token in f"{enum_name} {' '.join(members)}".lower() for token in tokens)
        ]
        if enum_hits:
            result["matching_enums"] = enum_hits[:20]
        return result

    def describe_enum(self, name: str | None = None) -> dict:
        if not name:
            return {"enums": sorted(self.enums)}
        lowered = name.lower()
        for enum_name, members in self.enums.items():
            if enum_name.lower() == lowered:
                return {"enum": enum_name, "members": members}
        matches = [
            enum_name
            for enum_name, members in self.enums.items()
            if lowered in enum_name.lower() or any(lowered in m.lower() for m in members)
        ]
        if not matches:
            raise CsiError("not_found", f"No enum matching {name!r}.")
        if len(matches) == 1:
            return {"enum": matches[0], "members": self.enums[matches[0]]}
        return {"matching_enums": sorted(matches)}

    def resolve_enum_member(self, text: str) -> int | None:
        """Map 'eUnits.kN_m_C' (or an unambiguous bare 'kN_m_C') to its value."""
        if "." in text:
            enum_name, _, member = text.partition(".")
            return self.enums.get(enum_name, {}).get(member)
        values = {members[text] for members in self.enums.values() if text in members}
        return values.pop() if len(values) == 1 else None


# ---------------------------------------------------------------------------
# Arguments and results
# ---------------------------------------------------------------------------


def coerce_value(value: Any, type_: str, index: ApiIndex | None = None) -> Any:
    """Smooth over JSON's loose typing so ctypes accepts the value.

    JSON does not distinguish 4 from 4.0, but ctypes refuses a float where an
    int is declared, so integral floats are narrowed and ints widened.
    """
    if type_.endswith("[]"):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [coerce_value(v, type_[:-2], index) for v in value]
        return value
    if type_ == "int":
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and index is not None:
            resolved = index.resolve_enum_member(value)
            return value if resolved is None else resolved
    elif type_ == "double":
        if isinstance(value, int) and not isinstance(value, bool):
            return float(value)
    elif type_ == "bool":
        if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
            return bool(value)
    elif type_ == "string":
        if value is None:
            return ""
    return value


def coerce_arguments(
    shown: str, member: dict, args: list, kwargs: dict, index: ApiIndex | None = None
) -> tuple[list, dict]:
    """Validate and convert call arguments against a function's signature."""
    inputs = [p for p in member["params"] if p["dir"] != "out"]
    signature = format_signature(shown, member)
    if len(args) > len(inputs):
        raise CsiError(
            "bad_request",
            f"{shown} takes at most {len(inputs)} arguments ({len(args)} given). Signature: {signature}",
        )
    by_lower_name = {p["name"].lower(): p for p in inputs}
    given_by_position = {p["name"] for p in inputs[: len(args)]}
    new_args = [coerce_value(value, inputs[i]["type"], index) for i, value in enumerate(args)]
    new_kwargs: dict[str, Any] = {}
    for key, value in kwargs.items():
        param = by_lower_name.get(str(key).lower())
        if param is None:
            raise CsiError("bad_request", f"{shown} has no input parameter named {key!r}. Signature: {signature}")
        if param["name"] in given_by_position or param["name"] in new_kwargs:
            raise CsiError("bad_request", f"{shown} got parameter {param['name']!r} more than once.")
        new_kwargs[param["name"]] = coerce_value(value, param["type"], index)
    missing = [
        p["name"]
        for position, p in enumerate(inputs)
        if p["dir"] == "in" and not p.get("optional") and position >= len(args) and p["name"] not in new_kwargs
    ]
    if missing:
        raise CsiError(
            "bad_request",
            f"{shown} is missing required argument(s): {', '.join(missing)}. Signature: {signature}",
        )
    return new_args, new_kwargs


def shape_result(shown: str, member: dict | None, raw: Any, seconds: float = 0.0) -> dict:
    """Label a raw COM return value: named ref outputs plus the return value.

    comtypes returns ``[ref outputs in declaration order..., return value]``,
    or a bare value when a function has exactly one of those.
    """
    result: dict[str, Any] = {"method": shown}
    ret = None
    is_status = False
    if member is not None:
        out_names = [p["name"] for p in member["params"] if p["dir"] != "in"]
        has_return = member["returns"] != "void"
        expected = len(out_names) + has_return
        if expected <= 1:
            values = [raw][:expected]
        elif isinstance(raw, (list, tuple)) and len(raw) == expected:
            values = list(raw)
        else:
            values = None
        if values is None:
            result["raw"] = to_jsonable(raw)
        else:
            if has_return:
                ret = result["ret"] = to_jsonable(values[-1])
            if out_names:
                result["outputs"] = {name: to_jsonable(value) for name, value in zip(out_names, values)}
            is_status = returns_status(member)
    elif isinstance(raw, (list, tuple)):
        # No type information: by convention the return value comes last.
        result["raw"] = to_jsonable(raw)
        ret = to_jsonable(raw[-1]) if raw else None
        is_status = True
    else:
        ret = result["ret"] = to_jsonable(raw)
        is_status = not shown.rsplit(".", 1)[-1].startswith(("Count", "Get"))

    failed = is_status and isinstance(ret, int) and not isinstance(ret, bool) and ret != 0
    result["ok"] = not failed
    if failed:
        result["error"] = f"CSiBridge returned nonzero status {ret}: the call failed."
    if seconds >= 1.0:
        result["seconds"] = round(seconds, 2)
    return result
