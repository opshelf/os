from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict
from perpetual_os1.common import AxisCoordinate

# ----------------------------
# Types & Schema definitions
# ----------------------------

class MotorParam(TypedDict, total=False):
    motor_id: Any
    motor_name: str
    Ip: int
    rpm: int
    dir: int
    mpr: float
    home_rpm: int
    accel: int
    ret_rpm: int
    lim: float # max travel in mm

class GridCell(TypedDict, total=False):
    id: str
    coord: List[float]  # stored as JSON array [x, y]
    size: List[float]   # stored as JSON array [w, h]
    meta: Dict[str, Any]

class RobotParams(TypedDict, total=False):
    idle_position: AxisCoordinate
    pickup_location: AxisCoordinate
    SLIDE_EXTENSION: int
    GRIPPER_EXTENSION_PICK: int
    GRIPPER_EXTENSION_PLACE: int
    REVERSE: float
    JOG: int
    PXMM: float # pixels per mm movement
    PPR: int # pulse per revolution

class RobotState(TypedDict):
    """Root JSON document layout."""
    motors: List[MotorParam]
    grid: Dict[str, GridCell]
    robot: RobotParams
    status: str

# ----------------------------
# Creation & I/O
# ----------------------------

def new_state(
    *,
    motors: Optional[List[MotorParam]] = None,
    robot: Optional[RobotParams] = None,
    status: str = "idle",
) -> RobotState:
    default_robot: RobotParams = {
        "SLIDE_EXTENSION": 50,
        "GRIPPER_EXTENSION_PICK": 171,
        "GRIPPER_EXTENSION_PLACE": 165,
        "REVERSE": 180.5,
        "JOG": 30,
        "PXMM": 0.22,
        "PPR": 10000,
    }
    seeded_robot = default_robot.copy()
    if robot:
        seeded_robot.update(robot)
    return {
        "motors": list(motors or []),
        "grid": {},
        "robot": dict(seeded_robot),
        "status": status,
    }


def load_state(path: str | Path) -> RobotState:
    """Load a state JSON document from disk. Raises on parse errors."""
    data = json.loads(Path(path).read_text())
    _validate_root(data)
    return data


def save_state(state: RobotState, path: str | Path, *, pretty: bool = True) -> None:
    """Save the state to disk."""
    _validate_root(state)
    p = Path(path)
    if pretty:
        p.write_text(json.dumps(state, indent=2, sort_keys=False))
    else:
        p.write_text(json.dumps(state, separators=(",", ":")))

# ----------------------------
# Status helpers
# ----------------------------

def get_status(state: RobotState) -> str:
    return state.get("status", "")


def set_status(state: RobotState, value: str) -> None:
    state["status"] = str(value)

# ----------------------------
# Robot parameter helpers
# ----------------------------

def upsert_robot_params(state: RobotState, **params: Any) -> None:
    """Merge/overwrite robot parameters with provided key/values."""
    state["robot"].update(params)


def get_robot_params(state: RobotState) -> RobotParams:
    return state["robot"]

# ----------------------------
# Motor helpers
# ----------------------------

def _normalize_id(value: Any) -> str:
    return str(value)

def _get_motor_identifier(m: Dict[str, Any]) -> Optional[str]:
    """Return a normalized id for a motor dict, checking motor_id then id."""
    if "motor_id" in m and m["motor_id"] is not None:
        return _normalize_id(m["motor_id"])
    return None

def list_motors(state: RobotState) -> List[MotorParam]:
    return state["motors"]

def find_motor(state: RobotState, motor_id: Any) -> Optional[MotorParam]:
    mid = _normalize_id(motor_id)
    for m in state["motors"]:
        ident = _get_motor_identifier(m)
        if ident == mid:
            return m
    return None

def upsert_motor(state: RobotState, motor_id: Any, **fields: Any) -> MotorParam:
    """Insert a new motor or update an existing one by identifier.

    Example:
        upsert_motor(state, 5, motor_name="x_axis", rpm=0xF0)

    Notes:
        - The canonical identifier key is `motor_id`. A legacy `id` key is
          still recognized but `upsert_motor` will always set `motor_id`.
    """
    mid = _normalize_id(motor_id)
    existing = find_motor(state, mid)
    if existing is None:
        new_entry: MotorParam = {"motor_id": mid}
        new_entry.update(fields)
        state["motors"].append(new_entry)
        return new_entry
    else:
        existing.update(fields)
        existing["motor_id"] = mid  # normalize id key
        return existing


def remove_motor(state: RobotState, motor_id: Any) -> bool:
    mid = _normalize_id(motor_id)
    motors = state["motors"]
    for i, m in enumerate(motors):
        ident = _get_motor_identifier(m)
        if ident == mid:
            del motors[i]
            return True
    return False

# --------------------
# Motor Management
# --------------------

def _update_motor_field(state: RobotState, motor_name: str, field: str, value: Any) -> bool:
    for m in state["motors"]:
        if m.get("motor_name") == motor_name:
            m[field] = value
            return True
    return False
        
def _get_motor_field(state: RobotState, motor_name: str, field: str) -> Optional[Any]:
    for m in state["motors"]:
        if m.get("motor_name") == motor_name:
            return m.get(field)
    return None

def add_motor(state: RobotState, motor_name: str, motor_params: Dict[str, Any]):
    """Add or update a motor by name."""
    state["motors"][motor_name] = motor_params
    state.save()

def get_motor(state: RobotState, motor_name: str) -> Optional[Dict[str, Any]]:
    return state["motors"].get(motor_name)

# Typed setters/getters for each motor parameter

def get_motor_params(state: RobotState, motor_name: str) -> Optional[MotorParam]:
    return next((m for m in state["motors"] if m.get("motor_name") == motor_name), None)

def set_motor_id(state: RobotState, motor_name: str, value: int):
    _update_motor_field(state, motor_name, "motor_id", value)

def get_motor_id(state: RobotState, motor_name: str) -> Optional[int]:
    return _get_motor_field(state, motor_name, "motor_id")

def set_ip(state: RobotState, motor_name: str, value: int):
    _update_motor_field(state, motor_name, "Ip", value)

def get_ip(state: RobotState, motor_name: str) -> Optional[int]:
    return _get_motor_field(state, motor_name, "Ip")

def set_rpm(state: RobotState, motor_name: str, value: int):
    _update_motor_field(state, motor_name, "rpm", value)

def get_rpm(state: RobotState, motor_name: str) -> Optional[int]:
    return _get_motor_field(state, motor_name, "rpm")

def set_dir(state: RobotState, motor_name: str, value: int):
    _update_motor_field(state, motor_name, "dir", value)

def get_dir(state: RobotState, motor_name: str) -> Optional[int]:
    return _get_motor_field(state, motor_name, "dir")

def set_mpr(state: RobotState, motor_name: str, value: int):
    _update_motor_field(state, motor_name, "mpr", value)

def get_mpr(state: RobotState, motor_name: str) -> Optional[int]:
    return _get_motor_field(state, motor_name, "mpr")

def set_home_rpm(state: RobotState, motor_name: str, value: int):
    _update_motor_field(state, motor_name, "home_rpm", value)

def get_home_rpm(state: RobotState, motor_name: str) -> Optional[int]:
    return _get_motor_field(state, motor_name, "home_rpm")

def set_accel(state: RobotState, motor_name: str, value: int):
    _update_motor_field(motor_name, "accel", value)

def get_accel(state: RobotState, motor_name: str) -> Optional[int]:
    return state._get_motor_field(state, motor_name, "accel")

def set_ret_rpm(state: RobotState, motor_name: str, value: int):
    _update_motor_field(state, motor_name, "ret_rpm", value)

def get_ret_rpm(state: RobotState, motor_name: str) -> Optional[int]:
    state._get_motor_field(state, motor_name, "ret_rpm")

# ----------------------------
# Grid helpers (dynamically scaling dictionary)
# ----------------------------

def upsert_grid_cell(
    state: RobotState,
    cell_id: Any,
    coord: Tuple[float, float],
    size: Tuple[float, float],
    *,
    meta: Optional[Dict[str, Any]] = None,
) -> GridCell:
    """Insert or update a grid cell.

    Args:
        cell_id: unique identifier (coerced to str)
        coord: (x, y)
        size: (w, h)
        meta: optional free-form attributes
    """
    cid = _normalize_id(cell_id)
    cell: GridCell = {
        "id": cid,
        "coord": [float(coord[0]), float(coord[1])],
        "size": [float(size[0]), float(size[1])],
    }
    if meta is not None:
        cell["meta"] = dict(meta)
    state["grid"][cid] = cell
    return cell


def get_grid_cell(state: RobotState, cell_id: Any) -> Optional[GridCell]:
    return state["grid"].get(_normalize_id(cell_id))

def remove_grid_cell(state: RobotState, cell_id: Any) -> bool:
    return state["grid"].pop(_normalize_id(cell_id), None) is not None

def generate_grid(
    state: RobotState,
    *,
    rows: int,
    cols: int,
    origin: Tuple[float, float] = (0.0, 0.0),
    spacing: Tuple[float, float] = (1.0, 1.0),
    cell_size: Tuple[float, float] = (1.0, 1.0),
    id_prefix: str = "C",
    overwrite: bool = False,
) -> None:
    """Populate the grid dictionary with a regular lattice.

    The grid scales to any rows/cols you specify. Cell IDs are generated as
    f"{id_prefix}{r}_{c}". Existing cells are left intact unless overwrite=True.
    """
    ox, oy = origin
    sx, sy = spacing
    for r in range(rows):
        for c in range(cols):
            cid = f"{id_prefix}{r}_{c}"
            if not overwrite and cid in state["grid"]:
                continue
            x = ox + c * sx
            y = oy + r * sy
            upsert_grid_cell(state, cid, (x, y), cell_size)


def search_grid(grid: Dict[str, GridCell], cell_id: str) -> Optional[Tuple[float, float]]:
    """Return coordinates for a grid cell by ID."""
    cell = grid.get(cell_id)  # type: Optional[GridCell]
    if cell is None:
        return None  # or raise KeyError if you want strict behavior
    return cell.get("coord") 

def find_first_available_cell(grid: Dict[str, GridCell]) -> Optional[GridCell]:
    for cell_key, cell in grid.items():
        if cell.get("id") is None:
            return cell
    return None

# ----------------------------
# Validation
# ----------------------------

def _validate_root(data: Dict[str, Any]) -> None:
    """Lightweight schema checks; raises ValueError on problems.

    Accepts motors that contain either `motor_id` or legacy `id`.
    """
    if not isinstance(data, dict):
        raise ValueError("Root must be a JSON object (dict).")

    # Top-level keys
    for key in ("motors", "grid", "robot", "status"):
        if key not in data:
            raise ValueError(f"Missing top-level key: {key}")

    # motors
    motors = data["motors"]
    if not isinstance(motors, list):
        raise ValueError("'motors' must be a list")
    for m in motors:
        if not isinstance(m, dict):
            raise ValueError("Each motor must be a dict")
        if ("motor_id" not in m or m["motor_id"] in (None, "")) and (
            "id" not in m or m["id"] in (None, "")
        ):
            raise ValueError("Each motor must include 'motor_id' (or legacy 'id')")
        # Basic type checks for Beb's keys (if present)
        # for k in ("Ip", "rpm", "dir", "mpr", "home_rpm", "accel", "ret_rpm"):
        #     if k in m and not isinstance(m[k], float):
        #         raise ValueError(f"motor field '{k}' must be float when present")

    # grid
    grid = data["grid"]
    if not isinstance(grid, dict):
        raise ValueError("'grid' must be a dict")
    for k, cell in grid.items():
        if not isinstance(cell, dict):
            raise ValueError(f"Grid cell '{k}' must be a dict")
        for req in ("id", "coord", "size"):
            if req not in cell:
                raise ValueError(f"Grid cell '{k}' missing required key '{req}'")
        _ensure_num2(cell["coord"], name=f"grid[{k}].coord")
        _ensure_num2(cell["size"], name=f"grid[{k}].size")

    # robot
    if not isinstance(data["robot"], dict):
        raise ValueError("'robot' must be a dict")
    # sanity check known constants (if present)
    r = data["robot"]
    _ensure_opt_int(r, "SLIDE_EXTENSION")
    _ensure_opt_int(r, "GRIPPER_EXTENSION_PICK")
    _ensure_opt_int(r, "GRIPPER_EXTENSION_PLACE")
    _ensure_opt_float(r, "REVERSE")
    _ensure_opt_int(r, "JOG")
    _ensure_opt_float(r, "PXMM")
    _ensure_opt_int(r, "PPR")

    # status
    if not isinstance(data["status"], str):
        raise ValueError("'status' must be a string")


def _ensure_num2(val: Any, *, name: str) -> None:
    if not (isinstance(val, list) and len(val) == 2 and all(isinstance(x, (int, float)) for x in val)):
        raise ValueError(f"{name} must be a 2-item numeric list, e.g. [x, y]")


def _ensure_opt_int(d: Dict[str, Any], key: str) -> None:
    if key in d and not isinstance(d[key], int):
        raise ValueError(f"robot field '{key}' must be int when present")


def _ensure_opt_float(d: Dict[str, Any], key: str) -> None:
    if key in d and not isinstance(d[key], (int, float)):
        raise ValueError(f"robot field '{key}' must be float when present")