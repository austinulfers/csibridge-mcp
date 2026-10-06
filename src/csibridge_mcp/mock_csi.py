"""An in-memory imitation of a small slice of the CSiBridge API.

CSiBridge only runs on Windows, so this stands in for it when developing and
testing elsewhere. It is deliberately shaped like the classes comtypes
generates from CSI's type library:

* every interface class carries a ``_methods_`` list in comtypes' format
  (parameter names, ctypes-like types, in/out flags), so the real
  introspection code runs against it unchanged;
* calling a function follows comtypes' conventions: ``ref`` parameters may be
  omitted, arguments are type-checked the way ctypes checks them, and the
  result is ``[ref outputs..., return value]`` (or a bare value if there is
  only one).

The signatures follow CSI's published API, but only a few dozen functions
exist here and the numbers it produces are placeholders, not structural
results. The real signatures always come from the installed CSiBridge.
"""
from __future__ import annotations

import ast
import ctypes
import os
import time

PF_IN, PF_OUT, PF_RETVAL, PF_OPT = 1, 2, 8, 16
_REQUIRED = object()


class MockDisconnected(Exception):
    """Raised by every call once the fake application has been closed."""


# -- stand-ins for the ctypes/comtypes types found in generated wrappers -----


def _simple(name: str, code: str) -> type:
    return type(name, (), {"_type_": code})


BSTR = _simple("BSTR", "X")
VARIANT_BOOL = _simple("VARIANT_BOOL", "v")
HRESULT = _simple("HRESULT", "l")
_BASE_TYPES = {"double": ctypes.c_double, "int": ctypes.c_int, "string": BSTR, "bool": VARIANT_BOOL}
_type_cache: dict[tuple, type] = {}


def _cached(key: tuple, build) -> type:
    if key not in _type_cache:
        _type_cache[key] = build()
    return _type_cache[key]


def _pointer(target: type) -> type:
    return _cached(("ptr", target), lambda: type(f"LP_{target.__name__}", (), {"_type_": target}))


def _safearray(item: type) -> type:
    def build():
        array = type(f"SAFEARRAY_{item.__name__}", (), {})
        return type(f"LP_SAFEARRAY_{item.__name__}", (), {"_type_": array, "_itemtype_": item})

    return _cached(("array", item), build)


def _interface_pointer(interface: type) -> type:
    return _cached(
        ("iface", interface),
        lambda: type(f"POINTER({interface.__name__})", (), {"_type_": interface, "__com_interface__": interface}),
    )


def _ctype(type_: str) -> type:
    if type_.endswith("[]"):
        return _safearray(_BASE_TYPES[type_[:-2]])
    return _BASE_TYPES[type_]


# -- declaring functions ------------------------------------------------------


class _Param:
    def __init__(self, name: str, type_: str, direction: str, default):
        self.name, self.type, self.dir, self.default = name, type_, direction, default


def _parse_signature(signature: str) -> tuple[list[_Param], str]:
    """Parse ``"Name: string, Count: ref int, All: bool = False -> int"``."""
    head, _, returns = signature.partition("->")
    params = []
    for chunk in (c.strip() for c in head.split(",")):
        if not chunk:
            continue
        name, _, rest = chunk.partition(":")
        rest, has_default, default_text = rest.partition("=")
        words = rest.split()
        direction = words.pop(0) if words[0] in ("ref", "out") else "in"
        default = ast.literal_eval(default_text.strip()) if has_default else _REQUIRED
        params.append(_Param(name.strip(), words[0], direction, default))
    return params, returns.strip() or "int"


def _com_spec(name: str, params: list[_Param], returns: str) -> tuple:
    """Build a ``_methods_`` entry: (restype, name, argtypes, paramflags, idlflags, doc)."""
    argtypes, paramflags = [], []
    for param in params:
        if param.dir == "in":
            argtypes.append(_ctype(param.type))
            flags = PF_IN
        else:
            argtypes.append(_pointer(_ctype(param.type)))
            flags = PF_OUT | (PF_IN if param.dir == "ref" else 0)
        if param.default is _REQUIRED:
            paramflags.append((flags, param.name))
        else:
            paramflags.append((flags | PF_OPT, param.name, param.default))
    if returns != "void":
        argtypes.append(_pointer(_ctype(returns)))
        paramflags.append((PF_OUT | PF_RETVAL, "pRetVal"))
    return (HRESULT, name, tuple(argtypes), tuple(paramflags), (), None)


def _check(value, type_: str):
    """Accept or reject an argument the way ctypes would."""
    if type_.endswith("[]"):
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"expected a sequence for {type_}, got {type(value).__name__}")
        return tuple(_check(v, type_[:-2]) for v in value)
    if type_ == "int":
        if not isinstance(value, int):
            raise TypeError(f"'{type(value).__name__}' object cannot be interpreted as an integer")
        return int(value)
    if type_ == "double":
        if not isinstance(value, (int, float)):
            raise TypeError(f"must be real number, not {type(value).__name__}")
        return float(value)
    if type_ == "string":
        if value is not None and not isinstance(value, str):
            raise TypeError(f"unicode string expected instead of {type(value).__name__} instance")
        return value or ""
    if not isinstance(value, (bool, int)):
        raise TypeError(f"bool expected instead of {type(value).__name__} instance")
    return bool(value)


def _empty(type_: str):
    return () if type_.endswith("[]") else {"double": 0.0, "int": 0, "string": "", "bool": False}[type_]


def _comtypes_like(name: str, function, params: list[_Param], returns: str):
    """Wrap an implementation so it is called, and returns, as comtypes would."""
    inputs = [p for p in params if p.dir != "out"]
    expected = sum(p.dir != "in" for p in params) + (returns != "void")

    def method(self, *args, **kwargs):
        if not self._app.running:
            raise MockDisconnected("The RPC server is unavailable.")
        if len(args) > len(inputs):
            raise TypeError(f"call takes at most {len(inputs)} arguments ({len(args)} given)")
        values = {}
        for position, param in enumerate(inputs):
            if position < len(args):
                values[param.name] = _check(args[position], param.type)
            elif param.name in kwargs:
                values[param.name] = _check(kwargs.pop(param.name), param.type)
            elif param.dir == "ref":
                values[param.name] = _empty(param.type)  # comtypes makes an empty one
            elif param.default is not _REQUIRED:
                values[param.name] = param.default
            else:
                raise TypeError(f"required argument '{param.name}' missing")
        if kwargs:
            raise TypeError(f"{name}() got an unexpected keyword argument '{next(iter(kwargs))}'")
        result = function(self, **values)
        outputs = result if isinstance(result, tuple) else (result,)
        assert len(outputs) == expected, f"mock {name} returned {len(outputs)} values, expected {expected}"
        outputs = [tuple(v) if isinstance(v, list) else v for v in outputs]
        return outputs[0] if expected == 1 else outputs

    method.__name__ = name
    return method


def api(signature: str = ""):
    """Declare a mock API function.

    The implementation receives every ``in``/``ref`` parameter by name and
    returns ``(ref outputs in order..., return value)``.
    """

    def decorate(function):
        function._api_signature = signature
        return function

    return decorate


class MockInterface:
    """Base for fake COM interfaces; builds ``_methods_`` from @api functions."""

    _methods_: list = []
    _children_: dict[str, type] = {}

    def __init__(self, app: MockApplication):
        self._app = app
        self._m = app.model

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        specs = []
        for name, function in list(vars(cls).items()):
            signature = getattr(function, "_api_signature", None)
            if signature is None:
                continue
            params, returns = _parse_signature(signature)
            specs.append(_com_spec(name, params, returns))
            setattr(cls, name, _comtypes_like(name, function, params, returns))
        for prop, child in cls._children_.items():
            pointer = _pointer(_interface_pointer(child))
            specs.append((HRESULT, f"_get_{prop}", (pointer,), ((PF_OUT | PF_RETVAL, "pRetVal"),), ("propget",), None))
            setattr(cls, prop, property(lambda self, _child=child: _child(self._app)))
        cls._methods_ = specs


# -- the fake model -----------------------------------------------------------

ENUMS = {
    "eUnits": {
        "lb_in_F": 1, "lb_ft_F": 2, "kip_in_F": 3, "kip_ft_F": 4, "kN_mm_C": 5, "kN_m_C": 6,
        "kgf_mm_C": 7, "kgf_m_C": 8, "N_mm_C": 9, "N_m_C": 10, "Ton_mm_C": 11, "Ton_m_C": 12,
        "kN_cm_C": 13, "kgf_cm_C": 14, "N_cm_C": 15, "Ton_cm_C": 16,
    },
    "eMatType": {
        "Steel": 1, "Concrete": 2, "NoDesign": 3, "Aluminum": 4, "ColdFormed": 5, "Rebar": 6, "Tendon": 7,
    },
    "eLoadPatternType": {
        "Dead": 1, "SuperDead": 2, "Live": 3, "ReduceLive": 4, "Quake": 5, "Wind": 6, "Snow": 7, "Other": 8,
    },
    "eItemType": {"Objects": 0, "Group": 1, "SelectedObjects": 2},
    "eItemTypeElm": {"ObjectElm": 0, "Element": 1, "GroupElm": 2, "SelectionElm": 3},
}  # fmt: skip


def _text(number: float) -> str:
    return f"{number:g}"


class _Model:
    def __init__(self):
        self.units = 6
        self.reset()

    def reset(self) -> None:
        self.filename = ""
        self.locked = False
        self.analyzed = False
        self.materials: dict[str, int] = {}
        self.frame_props: dict[str, str] = {}
        self.points: dict[str, tuple[float, float, float]] = {}
        self.restraints: dict[str, tuple] = {}
        self.frames: dict[str, tuple[str, str, str]] = {}  # name -> (point i, point j, section)
        self.load_patterns: dict[str, tuple[int, float]] = {"DEAD": (1, 1.0)}
        self.output_cases: set[str] = set()
        self.pending_edits: dict[str, tuple[list[str], list[list[str]]]] = {}

    @staticmethod
    def new_name(taken) -> str:
        """The next free number, counted separately for each kind of object."""
        number = len(taken) + 1
        while str(number) in taken:
            number += 1
        return str(number)

    def point_at(self, x: float, y: float, z: float, name: str = "") -> str:
        for existing, coords in self.points.items():
            if coords == (x, y, z):
                return existing
        name = name or self.new_name(self.points)
        self.points[name] = (x, y, z)
        return name

    def displacement(self, point: str) -> float:
        """A placeholder 'result' so result plumbing has numbers to carry."""
        return -0.001 * (list(self.points).index(point) + 1) * len(self.frames)

    def tables(self) -> dict[str, tuple[list[str], list[list[str]], int]]:
        """Database tables as key -> (fields, rows, import type)."""
        tables = {
            "Joint Coordinates": (
                ["Joint", "CoordSys", "CoordType", "XorR", "Y", "Z"],
                [[n, "GLOBAL", "Cartesian", _text(x), _text(y), _text(z)] for n, (x, y, z) in self.points.items()],
                2,
            ),
            "Connectivity - Frame": (
                ["Frame", "JointI", "JointJ"],
                [[n, i, j] for n, (i, j, _) in self.frames.items()],
                2,
            ),
            "Frame Section Assignments": (
                ["Frame", "AnalSect"],
                [[n, section] for n, (_, _, section) in self.frames.items()],
                2,
            ),
            "Material Properties 01 - General": (
                ["Material", "Type"],
                [[n, str(kind)] for n, kind in self.materials.items()],
                2,
            ),
            "Load Pattern Definitions": (
                ["LoadPat", "DesignType", "SelfWtMult"],
                [[n, str(kind), _text(mult)] for n, (kind, mult) in self.load_patterns.items()],
                2,
            ),
            "Joint Displacements": (
                ["Joint", "OutputCase", "U1", "U2", "U3"],
                [
                    [point, case, "0", "0", _text(self.displacement(point))]
                    for point in self.points
                    for case in self.load_patterns
                ]
                if self.analyzed
                else [],
                0,
            ),
        }
        return tables


class MockApplication:
    """A fake running CSiBridge. ``api_object`` is its ``SapObject``."""

    def __init__(self):
        self.running = True
        self.model = _Model()
        # Lets tests exercise long-running-call handling.
        self.analysis_seconds = float(os.environ.get("CSIBRIDGE_MOCK_ANALYSIS_SECONDS", "0"))
        self.api_object = cOAPI(self)


# -- the fake API tree --------------------------------------------------------


class cFile(MockInterface):
    @api()
    def NewBlank(self):
        self._m.reset()
        return 0

    @api("FileName: string")
    def OpenFile(self, FileName):
        if not FileName.lower().endswith(".bdb"):
            return 1
        self._m.reset()
        self._m.filename = FileName
        return 0

    @api("FileName: string = ''")
    def Save(self, FileName):
        self._m.filename = FileName or self._m.filename
        return 0 if self._m.filename else 1


class cPropMaterial(MockInterface):
    @api("Name: string, MatType: int, Color: int = -1, Notes: string = '', GUID: string = ''")
    def SetMaterial(self, Name, MatType, Color, Notes, GUID):
        if self._m.locked or MatType not in ENUMS["eMatType"].values():
            return 1
        self._m.materials[Name] = MatType
        return 0

    @api("NumberNames: ref int, MyName: ref string[]")
    def GetNameList(self, NumberNames, MyName):
        return len(self._m.materials), list(self._m.materials), 0


class cPropFrame(MockInterface):
    @api("Name: string, MatProp: string, T3: double, T2: double, Color: int = -1, Notes: string = '', GUID: string = ''")
    def SetRectangle(self, Name, MatProp, T3, T2, Color, Notes, GUID):
        if self._m.locked or MatProp not in self._m.materials or T3 <= 0 or T2 <= 0:
            return 1
        self._m.frame_props[Name] = MatProp
        return 0

    @api("NumberNames: ref int, MyName: ref string[], PropType: int = 0")
    def GetNameList(self, NumberNames, MyName, PropType):
        return len(self._m.frame_props), list(self._m.frame_props), 0


class cPointObj(MockInterface):
    @api(
        "X: double, Y: double, Z: double, Name: ref string, UserName: string = '', "
        "CSys: string = 'Global', MergeOff: bool = False, MergeNumber: int = 0"
    )
    def AddCartesian(self, X, Y, Z, Name, UserName, CSys, MergeOff, MergeNumber):
        if self._m.locked or UserName in self._m.points:
            return "", 1
        return self._m.point_at(X, Y, Z, UserName), 0

    @api("Name: string, X: ref double, Y: ref double, Z: ref double, CSys: string = 'Global'")
    def GetCoordCartesian(self, Name, X, Y, Z, CSys):
        if Name not in self._m.points:
            return 0.0, 0.0, 0.0, 1
        return (*self._m.points[Name], 0)

    @api("NumberNames: ref int, MyName: ref string[]")
    def GetNameList(self, NumberNames, MyName):
        return len(self._m.points), list(self._m.points), 0

    @api("Name: string, Value: ref bool[], ItemType: int = 0")
    def SetRestraint(self, Name, Value, ItemType):
        if self._m.locked or Name not in self._m.points or len(Value) != 6:
            return Value, 1
        self._m.restraints[Name] = Value
        return Value, 0

    @api("Name: string, Value: ref bool[]")
    def GetRestraint(self, Name, Value):
        if Name not in self._m.points:
            return (), 1
        return self._m.restraints.get(Name, (False,) * 6), 0

    @api()
    def Count(self):
        return len(self._m.points)


class cFrameObj(MockInterface):
    @api(
        "XI: double, YI: double, ZI: double, XJ: double, YJ: double, ZJ: double, Name: ref string, "
        "PropName: string = 'Default', UserName: string = '', CSys: string = 'Global'"
    )
    def AddByCoord(self, XI, YI, ZI, XJ, YJ, ZJ, Name, PropName, UserName, CSys):
        if self._m.locked or UserName in self._m.frames or (XI, YI, ZI) == (XJ, YJ, ZJ):
            return "", 1
        name = UserName or self._m.new_name(self._m.frames)
        self._m.frames[name] = (self._m.point_at(XI, YI, ZI), self._m.point_at(XJ, YJ, ZJ), PropName)
        return name, 0

    @api("Point1: string, Point2: string, Name: ref string, PropName: string = 'Default', UserName: string = ''")
    def AddByPoint(self, Point1, Point2, Name, PropName, UserName):
        points = self._m.points
        if self._m.locked or Point1 not in points or Point2 not in points or UserName in self._m.frames:
            return "", 1
        name = UserName or self._m.new_name(self._m.frames)
        self._m.frames[name] = (Point1, Point2, PropName)
        return name, 0

    @api("Name: string, Point1: ref string, Point2: ref string")
    def GetPoints(self, Name, Point1, Point2):
        if Name not in self._m.frames:
            return "", "", 1
        return (*self._m.frames[Name][:2], 0)

    @api("Name: string, PropName: ref string, SAuto: ref string")
    def GetSection(self, Name, PropName, SAuto):
        if Name not in self._m.frames:
            return "", "", 1
        return self._m.frames[Name][2], "", 0

    @api("NumberNames: ref int, MyName: ref string[]")
    def GetNameList(self, NumberNames, MyName):
        return len(self._m.frames), list(self._m.frames), 0

    @api("Name: string, ItemType: int = 0")
    def Delete(self, Name, ItemType):
        if self._m.locked or Name not in self._m.frames:
            return 1
        del self._m.frames[Name]
        return 0

    @api()
    def Count(self):
        return len(self._m.frames)


class cLoadPatterns(MockInterface):
    @api("Name: string, MyType: int, SelfWTMultiplier: double = 0.0, AddLoadCase: bool = True")
    def Add(self, Name, MyType, SelfWTMultiplier, AddLoadCase):
        if self._m.locked or Name in self._m.load_patterns:
            return 1
        self._m.load_patterns[Name] = (MyType, SelfWTMultiplier)
        return 0

    @api("NumberNames: ref int, MyName: ref string[]")
    def GetNameList(self, NumberNames, MyName):
        return len(self._m.load_patterns), list(self._m.load_patterns), 0


class cAnalyze(MockInterface):
    @api()
    def RunAnalysis(self):
        if not self._m.filename or not self._m.frames:
            return 1  # like CSiBridge, refuse to analyse an unsaved or empty model
        time.sleep(self._app.analysis_seconds)
        self._m.analyzed = self._m.locked = True
        return 0

    @api("NumberItems: ref int, CaseName: ref string[], Status: ref int[]")
    def GetCaseStatus(self, NumberItems, CaseName, Status):
        cases = list(self._m.load_patterns)
        return len(cases), cases, [4 if self._m.analyzed else 1] * len(cases), 0


class cAnalysisResultsSetup(MockInterface):
    @api()
    def DeselectAllCasesAndCombosForOutput(self):
        self._m.output_cases.clear()
        return 0

    @api("Name: string, Selected: bool = True")
    def SetCaseSelectedForOutput(self, Name, Selected):
        if Name not in self._m.load_patterns:
            return 1
        (self._m.output_cases.add if Selected else self._m.output_cases.discard)(Name)
        return 0


class cAnalysisResults(MockInterface):
    _children_ = {"Setup": cAnalysisResultsSetup}

    @api(
        "Name: string, ItemTypeElm: int, NumberResults: ref int, Obj: ref string[], Elm: ref string[], "
        "LoadCase: ref string[], StepType: ref string[], StepNum: ref double[], U1: ref double[], "
        "U2: ref double[], U3: ref double[], R1: ref double[], R2: ref double[], R3: ref double[]"
    )
    def JointDispl(self, Name, ItemTypeElm, NumberResults, Obj, Elm, LoadCase, StepType, StepNum, U1, U2, U3, R1, R2, R3):
        cases = sorted(self._m.output_cases)
        if not self._m.analyzed or Name not in self._m.points:
            return (0, *[()] * 11, 1)
        zeros = [0.0] * len(cases)
        names = [Name] * len(cases)
        u3 = [self._m.displacement(Name)] * len(cases)
        return len(cases), names, names, cases, [""] * len(cases), zeros, zeros, zeros, u3, zeros, zeros, zeros, 0


class cDatabaseTables(MockInterface):
    @api("NumberTables: ref int, TableKey: ref string[], TableName: ref string[], ImportType: ref int[]")
    def GetAvailableTables(self, NumberTables, TableKey, TableName, ImportType):
        available = {key: table for key, table in self._m.tables().items() if table[1]}
        return len(available), list(available), list(available), [t[2] for t in available.values()], 0

    @api(
        "NumberTables: ref int, TableKey: ref string[], TableName: ref string[], ImportType: ref int[], "
        "IsEmpty: ref bool[]"
    )
    def GetAllTables(self, NumberTables, TableKey, TableName, ImportType, IsEmpty):
        tables = self._m.tables()
        import_types = [t[2] for t in tables.values()]
        return len(tables), list(tables), list(tables), import_types, [not t[1] for t in tables.values()], 0

    @api(
        "TableKey: string, TableVersion: ref int, NumberFields: ref int, FieldKey: ref string[], "
        "FieldName: ref string[], Description: ref string[], UnitsString: ref string[], IsImportable: ref bool[]"
    )
    def GetAllFieldsInTable(self, TableKey, TableVersion, NumberFields, FieldKey, FieldName, Description, UnitsString, IsImportable):
        table = self._m.tables().get(TableKey)
        if table is None:
            return 0, 0, (), (), (), (), (), 1
        fields = table[0]
        return 1, len(fields), fields, fields, fields, [""] * len(fields), [table[2] != 0] * len(fields), 0

    def _read(self, table_key: str, wanted: tuple = ()):
        table = self._m.tables().get(table_key)
        if table is None:
            return None
        fields, rows, _ = table
        keep = [i for i, name in enumerate(fields) if not wanted or name in wanted]
        return [fields[i] for i in keep], len(rows), [row[i] for row in rows for i in keep]

    @api(
        "TableKey: string, FieldKeyList: ref string[], GroupName: string, TableVersion: ref int, "
        "FieldsKeysIncluded: ref string[], NumberRecords: ref int, TableData: ref string[]"
    )
    def GetTableForDisplayArray(self, TableKey, FieldKeyList, GroupName, TableVersion, FieldsKeysIncluded, NumberRecords, TableData):
        data = self._read(TableKey, tuple(f for f in FieldKeyList if f))
        if data is None:
            return FieldKeyList, 0, (), 0, (), 1
        return FieldKeyList, 1, *data, 0

    @api(
        "TableKey: string, GroupName: string, TableVersion: ref int, FieldsKeysIncluded: ref string[], "
        "NumberRecords: ref int, TableData: ref string[]"
    )
    def GetTableForEditingArray(self, TableKey, GroupName, TableVersion, FieldsKeysIncluded, NumberRecords, TableData):
        data = self._read(TableKey)
        if data is None or self._m.tables()[TableKey][2] == 0:
            return 0, (), 0, (), 1
        return 1, *data, 0

    @api(
        "TableKey: string, TableVersion: ref int, FieldsKeysIncluded: ref string[], NumberRecords: int, "
        "TableData: ref string[]"
    )
    def SetTableForEditingArray(self, TableKey, TableVersion, FieldsKeysIncluded, NumberRecords, TableData):
        table = self._m.tables().get(TableKey)
        width = len(FieldsKeysIncluded)
        if table is None or table[2] == 0 or not width or len(TableData) != width * NumberRecords:
            return TableVersion, FieldsKeysIncluded, TableData, 1
        rows = [list(TableData[i : i + width]) for i in range(0, len(TableData), width)]
        self._m.pending_edits[TableKey] = (list(FieldsKeysIncluded), rows)
        return TableVersion, FieldsKeysIncluded, TableData, 0

    @api(
        "FillImportLog: bool, NumFatalErrors: ref int, NumErrorMsgs: ref int, NumWarnMsgs: ref int, "
        "NumInfoMsgs: ref int, ImportLog: ref string"
    )
    def ApplyEditedTables(self, FillImportLog, NumFatalErrors, NumErrorMsgs, NumWarnMsgs, NumInfoMsgs, ImportLog):
        edits, self._m.pending_edits = self._m.pending_edits, {}
        if self._m.locked:
            return 1, 0, 0, 0, "Model is locked; tables were not imported.", 1
        log = []
        for key, (fields, rows) in edits.items():
            if key != "Joint Coordinates":
                log.append(f"The mock cannot import table {key!r}.")
                continue
            try:
                records = [dict(zip(fields, row)) for row in rows]
                points = {r["Joint"]: (float(r["XorR"]), float(r["Y"]), float(r["Z"])) for r in records}
            except (KeyError, ValueError) as exc:
                log.append(f"Joint Coordinates: bad record ({exc}).")
                continue
            self._m.points = points
            self._m.frames = {n: f for n, f in self._m.frames.items() if f[0] in points and f[1] in points}
        return 0, len(log), 0, len(edits) - len(log), "\n".join(log) if FillImportLog else "", 0

    @api()
    def CancelTableEditing(self):
        self._m.pending_edits.clear()
        return 0

    @api("LoadCaseList: ref string[]")
    def SetLoadCasesSelectedForDisplay(self, LoadCaseList):
        return LoadCaseList, 0

    @api("LoadCombinationList: ref string[]")
    def SetLoadCombinationsSelectedForDisplay(self, LoadCombinationList):
        return LoadCombinationList, 0

    @api("LoadPatternList: ref string[]")
    def SetLoadPatternsSelectedForDisplay(self, LoadPatternList):
        return LoadPatternList, 0


class cView(MockInterface):
    @api("Window: int = 0, Zoom: bool = True")
    def RefreshView(self, Window, Zoom):
        return 0


class cSapModel(MockInterface):
    _children_ = {
        "File": cFile,
        "PropMaterial": cPropMaterial,
        "PropFrame": cPropFrame,
        "PointObj": cPointObj,
        "FrameObj": cFrameObj,
        "LoadPatterns": cLoadPatterns,
        "Analyze": cAnalyze,
        "Results": cAnalysisResults,
        "DatabaseTables": cDatabaseTables,
        "View": cView,
    }

    @api("Units: int = 3")
    def InitializeNewModel(self, Units):
        if Units not in ENUMS["eUnits"].values():
            return 1
        self._m.units = Units
        self._m.reset()
        return 0

    @api("Version: ref string, MyVersionNumber: ref double")
    def GetVersion(self, Version, MyVersionNumber):
        return "0.0.0", 0.0, 0

    @api("ProgramName: ref string, ProgramVersion: ref string, ProgramLevel: ref string")
    def GetProgramInfo(self, ProgramName, ProgramVersion, ProgramLevel):
        return "CSiBridge (mock)", "0.0.0", "Mock", 0

    @api("IncludePath: bool = True -> string")
    def GetModelFilename(self, IncludePath):
        return self._m.filename if IncludePath else self._m.filename.replace("/", "\\").rsplit("\\", 1)[-1]

    @api()
    def GetPresentUnits(self):
        return self._m.units

    @api("Units: int")
    def SetPresentUnits(self, Units):
        if Units not in ENUMS["eUnits"].values():
            return 1
        self._m.units = Units
        return 0

    @api("-> bool")
    def GetModelIsLocked(self):
        return self._m.locked

    @api("LockIt: bool")
    def SetModelIsLocked(self, LockIt):
        self._m.locked = LockIt
        if not LockIt:
            self._m.analyzed = False  # unlocking discards results, as in CSiBridge
        return 0


class cOAPI(MockInterface):
    _children_ = {"SapModel": cSapModel}

    @api()
    def ApplicationStart(self):
        return 0

    @api("FileSave: bool")
    def ApplicationExit(self, FileSave):
        self._app.running = False
        return 0

    @api("-> double")
    def GetOAPIVersionNumber(self):
        return 1.0
