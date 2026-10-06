import ctypes

import pytest

from csibridge_mcp import mock_csi
from csibridge_mcp.errors import CsiError
from csibridge_mcp.introspect import (
    ApiIndex,
    coerce_arguments,
    coerce_value,
    display_path,
    format_signature,
    returns_status,
    shape_result,
    split_path,
    to_jsonable,
    type_name,
)


@pytest.fixture(scope="module")
def index():
    return ApiIndex(mock_csi.cOAPI, mock_csi.ENUMS)


def member(index, path):
    return index.member(split_path(path))


# -- type names -------------------------------------------------------------


def test_type_names_for_real_ctypes_types():
    # c_int is c_long on Windows; both must read as "int".
    assert type_name(ctypes.c_double) == "double"
    assert type_name(ctypes.c_int) == "int"
    assert type_name(ctypes.c_long) == "int"
    assert type_name(ctypes.c_wchar_p) == "string"
    assert type_name(ctypes.POINTER(ctypes.c_double)) == "double"
    assert type_name(ctypes.POINTER(ctypes.POINTER(ctypes.c_int))) == "int"
    assert type_name(None) == "void"


def test_type_names_for_comtypes_shaped_types():
    array = mock_csi._safearray(mock_csi.BSTR)
    assert type_name(array) == "string[]"
    assert type_name(mock_csi._pointer(array)) == "string[]"
    assert type_name(mock_csi._interface_pointer(mock_csi.cFrameObj)) == "cFrameObj"
    assert type_name(mock_csi.VARIANT_BOOL) == "bool"


# -- paths ------------------------------------------------------------------


def test_split_path_normalises_roots():
    assert split_path("SapModel.FrameObj.Count") == ["SapObject", "SapModel", "FrameObj", "Count"]
    assert split_path("SapObject.ApplicationStart") == ["SapObject", "ApplicationStart"]
    assert split_path("FrameObj.Count()") == ["SapObject", "SapModel", "FrameObj", "Count"]
    assert display_path(["SapObject", "SapModel", "File", "Save"]) == "SapModel.File.Save"
    assert display_path(["SapObject", "ApplicationStart"]) == "SapObject.ApplicationStart"


@pytest.mark.parametrize(
    "path",
    ["", None, 3, "SapModel.__class__", "SapModel.FrameObj.Release", "SapModel..Count", "SapModel.File.Save; x"],
)
def test_split_path_rejects_unsafe_or_malformed_paths(path):
    with pytest.raises(CsiError):
        split_path(path)


# -- the index --------------------------------------------------------------


def test_index_walks_the_whole_tree(index):
    assert index.paths["SapObject"] == "cOAPI"
    assert index.paths["SapObject.SapModel.Results.Setup"] == "cAnalysisResultsSetup"
    assert index.interface_at(split_path("SapModel.FrameObj")) == "cFrameObj"
    assert index.summary()["functions"] > 40


def test_signature_shows_directions_and_defaults(index):
    signature = format_signature("SapModel.FrameObj.AddByCoord", member(index, "SapModel.FrameObj.AddByCoord"))
    assert signature.startswith("SapModel.FrameObj.AddByCoord(XI: double, ")
    assert "Name: ref string" in signature
    assert 'CSys: string = "Global"' in signature
    assert signature.endswith("-> int")


def test_array_and_value_returns_are_described(index):
    get_names = member(index, "SapModel.PointObj.GetNameList")
    assert [(p["name"], p["type"], p["dir"]) for p in get_names["params"]] == [
        ("NumberNames", "int", "ref"),
        ("MyName", "string[]", "ref"),
    ]
    assert member(index, "SapModel.GetModelFilename")["returns"] == "string"
    assert member(index, "SapModel.GetModelIsLocked")["returns"] == "bool"


def test_returns_status_tells_status_codes_from_values(index):
    assert returns_status(member(index, "SapModel.SetPresentUnits"))
    assert returns_status(member(index, "SapModel.File.NewBlank"))
    assert returns_status(member(index, "SapModel.PointObj.GetNameList"))  # Get... with ref outputs
    assert not returns_status(member(index, "SapModel.GetPresentUnits"))  # returns the units
    assert not returns_status(member(index, "SapModel.FrameObj.Count"))  # returns the count
    assert not returns_status(member(index, "SapModel.GetModelIsLocked"))  # returns a bool


def test_describe_interface_function_and_tree(index):
    interface = index.describe("SapModel.Results")
    assert interface["sub_interfaces"] == ["SapModel.Results.Setup"]
    assert any(line.startswith("JointDispl(") for line in interface["functions"])

    filtered = index.describe("SapModel.FrameObj", name_filter="add")
    assert [line.split("(")[0] for line in filtered["functions"]] == ["AddByCoord", "AddByPoint"]

    function = index.describe("SapModel.FrameObj.GetPoints")
    assert function["outputs"] == ["Point1", "Point2"]
    assert function["return_is_status_code"] is True

    assert any(line.startswith("SapModel.DatabaseTables") for line in index.describe()["interfaces"])


def test_describe_unknown_function_suggests_close_names(index):
    with pytest.raises(CsiError) as error:
        index.describe("SapModel.FrameObj.AddByCoords")
    assert "AddByCoord" in error.value.message

    with pytest.raises(CsiError) as error:
        index.describe("SapModel.Frames.AddByCoord")
    assert "SapModel.FrameObj.AddByCoord" in error.value.message


def test_search_ranks_name_matches_first(index):
    result = index.search("frame add")
    assert result["matches"][0].startswith("SapModel.FrameObj.AddBy")
    assert result["total_matches"] == 2

    # Words can match the path or a parameter name, and enums are searched too.
    assert index.search("table editing")["total_matches"] == 3
    assert index.search("units")["matching_enums"] == ["eUnits"]
    assert index.search("nothing_like_this")["matches"] == []


def test_enum_lookup(index):
    assert index.describe_enum("eunits")["members"]["kN_m_C"] == 6
    assert index.describe_enum("Concrete")["enum"] == "eMatType"
    assert "eItemType" in index.describe_enum("item")["matching_enums"]
    assert "eUnits" in index.describe_enum()["enums"]
    assert index.resolve_enum_member("eUnits.kip_ft_F") == 4
    assert index.resolve_enum_member("kip_ft_F") == 4
    assert index.resolve_enum_member("eUnits.nope") is None


# -- arguments --------------------------------------------------------------


def test_coerce_value_bridges_json_and_ctypes(index):
    assert coerce_value(4.0, "int") == 4 and isinstance(coerce_value(4.0, "int"), int)
    assert coerce_value(4.5, "int") == 4.5  # left for ctypes to reject
    assert isinstance(coerce_value(3, "double"), float)
    assert coerce_value(1, "bool") is True
    assert coerce_value(None, "string") == ""
    assert coerce_value(None, "double[]") == []
    assert coerce_value([1, 2], "double[]") == [1.0, 2.0]
    assert coerce_value("eUnits.kN_m_C", "int", index) == 6
    assert coerce_value("not_an_enum", "int", index) == "not_an_enum"


def test_coerce_arguments_accepts_names_in_any_case(index):
    add = member(index, "SapModel.FrameObj.AddByCoord")
    args, kwargs = coerce_arguments("AddByCoord", add, [0, 0, 0], {"xj": 5, "YJ": 0, "zj": 0, "username": "B1"}, index)
    assert args == [0.0, 0.0, 0.0]
    assert kwargs == {"XJ": 5.0, "YJ": 0.0, "ZJ": 0.0, "UserName": "B1"}


def test_coerce_arguments_explains_mistakes(index):
    add = member(index, "SapModel.FrameObj.AddByCoord")
    with pytest.raises(CsiError, match="missing required argument.*ZJ"):
        coerce_arguments("AddByCoord", add, [0, 0, 0, 5, 0], {}, index)
    with pytest.raises(CsiError, match="no input parameter named 'Section'"):
        coerce_arguments("AddByCoord", add, [0, 0, 0, 5, 0, 0], {"Section": "R1"}, index)
    with pytest.raises(CsiError, match="more than once"):
        coerce_arguments("AddByCoord", add, [0, 0, 0, 5, 0, 0], {"xi": 1}, index)
    with pytest.raises(CsiError, match="at most 10 arguments"):
        coerce_arguments("AddByCoord", add, list(range(11)), {}, index)


# -- results ----------------------------------------------------------------


def test_shape_result_names_outputs(index):
    get_points = member(index, "SapModel.FrameObj.GetPoints")
    assert shape_result("GetPoints", get_points, ["1", "2", 0]) == {
        "method": "GetPoints",
        "ret": 0,
        "outputs": {"Point1": "1", "Point2": "2"},
        "ok": True,
    }
    failed = shape_result("GetPoints", get_points, ["", "", 1])
    assert failed["ok"] is False and "nonzero status 1" in failed["error"]


def test_shape_result_handles_bare_values(index):
    assert shape_result("Count", member(index, "SapModel.FrameObj.Count"), 7) == {"method": "Count", "ret": 7, "ok": True}
    assert shape_result("Save", member(index, "SapModel.File.Save"), 1)["ok"] is False
    assert shape_result("GetModelIsLocked", member(index, "SapModel.GetModelIsLocked"), True)["ok"] is True


def test_shape_result_without_or_against_type_information(index):
    # Unexpected shape for a known function: hand back what came out, unlabelled.
    odd = shape_result("GetPoints", member(index, "SapModel.FrameObj.GetPoints"), ["only-one"])
    assert odd["raw"] == ["only-one"] and "outputs" not in odd
    # No type information at all: the last element is the return value by convention.
    assert shape_result("X.GetNameList", None, (2, ("a", "b"), 0)) == {
        "method": "X.GetNameList",
        "raw": [2, ["a", "b"], 0],
        "ok": True,
    }
    assert shape_result("X.Delete", None, 1)["ok"] is False
    assert shape_result("X.Count", None, 3)["ok"] is True


def test_shape_result_reports_slow_calls(index):
    assert shape_result("Count", member(index, "SapModel.FrameObj.Count"), 7, seconds=2.345)["seconds"] == 2.35


def test_to_jsonable():
    assert to_jsonable((1, ("a", 2.5), None)) == [1, ["a", 2.5], None]
    assert to_jsonable(float("nan")) == "nan"
    assert to_jsonable(ctypes.c_double(1.5)) == 1.5
    assert to_jsonable(object()) == "<object>"
