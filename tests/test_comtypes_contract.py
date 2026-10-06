"""Checks our reading of comtypes against comtypes' own code.

The real backend cannot run off Windows, but what it assumes about comtypes
lives in one pure-Python module, ``comtypes._memberspec``: the format of the
``_methods_`` entries that introspection reads, and the wrapper that defines
how ``ref`` parameters are passed in and handed back. That module is loaded
here on its own, with the Windows-only rest of the package stubbed out, and
fed real ctypes types.
"""
import ctypes
import importlib.util
import sys
import types
from ctypes import POINTER, c_double, c_int
from pathlib import Path

import pytest

from csibridge_mcp.introspect import (
    _member_from_com_spec,
    _member_from_disp_spec,
    coerce_value,
    format_signature,
    shape_result,
)

HRESULT = ctypes.c_long


class BSTR(ctypes.c_wchar_p):
    def __ctypes_from_outparam__(self):  # the real BSTR returns its text too
        return self.value


class SAFEARRAY_c_double(ctypes.Structure):
    pass


# comtypes patches the element type onto the pointer-to-SAFEARRAY type.
LP_SAFEARRAY_c_double = POINTER(SAFEARRAY_c_double)
LP_SAFEARRAY_c_double._itemtype_ = c_double


class cFrameObj:
    """Stands in for an interface class; comtypes hangs it off the pointer type."""


POINTER_cFrameObj = type("POINTER(cFrameObj)", (ctypes.c_void_p,), {"__com_interface__": cFrameObj})


@pytest.fixture(scope="module")
def memberspec():
    found = importlib.util.find_spec("comtypes")
    if found is None:
        pytest.skip("comtypes is not installed")
    source = Path(found.submodule_search_locations[0]) / "_memberspec.py"

    package = types.ModuleType("comtypes")
    package.__path__ = []
    automation = types.ModuleType("comtypes.automation")
    automation.VARIANT = type("VARIANT", (ctypes.Structure,), {})
    stubs = {"comtypes": package, "comtypes.automation": automation}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location("comtypes_memberspec_under_test", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def test_method_spec_built_by_comtypes_is_read_correctly(memberspec):
    spec = memberspec.COMMETHOD(
        [memberspec.dispid(7), memberspec.helpstring("adds a frame")],
        HRESULT,
        "AddByCoord",
        (["in"], c_double, "XI"),
        (["in", "out"], POINTER(BSTR), "Name"),
        (["in", "optional"], BSTR, "PropName", "Default"),
        (["in", "out"], POINTER(LP_SAFEARRAY_c_double), "Offsets"),
        (["in", "optional"], c_int, "ItemType"),
        (["out", "retval"], POINTER(c_int), "pRetVal"),
    )
    member, child = _member_from_com_spec(spec)
    assert child is None
    assert format_signature("F", member) == (
        'F(XI: double, Name: ref string, PropName: string = "Default", '
        "Offsets: ref double[], ItemType: int = 0) -> int"
    )


def test_property_specs_built_by_comtypes(memberspec):
    getter = memberspec.COMMETHOD(
        ["propget"], HRESULT, "FrameObj", (["out", "retval"], POINTER(POINTER_cFrameObj), "pRetVal")
    )
    member, child = _member_from_com_spec(getter)
    assert (member["name"], member["kind"], member["returns"]) == ("FrameObj", "property", "cFrameObj")
    assert child is cFrameObj

    setter = memberspec.COMMETHOD(["propput"], HRESULT, "Visible", (["in"], c_int, "value"))
    assert _member_from_com_spec(setter) == (None, None)


def test_dispinterface_specs_built_by_comtypes(memberspec):
    method = memberspec.DISPMETHOD([memberspec.dispid(3)], c_int, "Count", (["in"], BSTR, "Name"))
    member, _ = _member_from_disp_spec(method)
    assert format_signature("Count", member) == "Count(Name: string) -> int"

    prop = memberspec.DISPPROPERTY([memberspec.dispid(4), "readonly"], BSTR, "Title")
    member, _ = _member_from_disp_spec(prop)
    assert (member["kind"], member["returns"]) == ("property", "string")


def test_ref_parameters_follow_comtypes_calling_convention(memberspec):
    """Omitted refs are created by comtypes, and come back in order before the return value."""
    spec = memberspec.COMMETHOD(
        [],
        HRESULT,
        "GetPoints",
        (["in"], BSTR, "Name"),
        (["in", "out"], POINTER(BSTR), "Point1"),
        (["in", "out"], POINTER(BSTR), "Point2"),
        (["out", "retval"], POINTER(c_int), "pRetVal"),
    )

    def com_call(self, *args, **kwargs):  # stands in for the underlying COM call
        assert args == ("F1",)
        kwargs["Point1"].value, kwargs["Point2"].value = "1", "2"
        return [None, None, 0]

    raw = memberspec._fix_inout_args(com_call, spec.argtypes, spec.paramflags)(object(), "F1")
    assert raw == ["1", "2", 0]
    member, _ = _member_from_com_spec(spec)
    assert shape_result("GetPoints", member, raw) == {
        "method": "GetPoints",
        "ret": 0,
        "outputs": {"Point1": "1", "Point2": "2"},
        "ok": True,
    }


def test_a_single_output_comes_back_bare(memberspec):
    spec = memberspec.COMMETHOD([], HRESULT, "GetCount", (["in", "out"], POINTER(c_int), "Count"))

    def com_call(self, *args, **kwargs):
        kwargs["Count"].value = 5
        return kwargs["Count"]  # with one output, ctypes hands back the buffer itself

    raw = memberspec._fix_inout_args(com_call, spec.argtypes, spec.paramflags)(object())
    assert raw == 5
    member, _ = _member_from_com_spec(spec)
    assert shape_result("GetCount", member, raw)["outputs"] == {"Count": 5}


def test_comtypes_rejects_floats_for_ints_unless_coerced(memberspec):
    # Why coerce_value exists: JSON clients send 4.0, and comtypes will not take it.
    with pytest.raises(TypeError):
        memberspec._prepare_parameter(4.0, c_int)
    assert memberspec._prepare_parameter(coerce_value(4.0, "int"), c_int).value == 4
    assert memberspec._prepare_parameter(coerce_value(4, "double"), c_double).value == 4.0
