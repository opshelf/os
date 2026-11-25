#!/usr/bin/env python3
"""
Paul Cherry Picker — FastAPI web interface for x, z, y motor control.
Run:  python3 cherrypicker_app.py
Open: http://localhost:8767
"""

import os
import sys

# fastapi/uvicorn/pymodbus/pyserial live in this project's venv, not system
# Python. The shebang above just does a PATH lookup for "python3" -- it
# can't activate a venv -- so running this file directly (or via a bare
# "python3 cherrypicker_app.py") under system Python fails with
# ModuleNotFoundError before it even gets to those imports. Re-exec under
# the venv's interpreter here, before anything else is imported, so the
# script "just works" either way. Falls through unchanged if the venv
# doesn't exist (e.g. on a machine that hasn't set one up) or if we're
# already running under it -- no infinite loop.
#
# venv/bin/python3 is normally just a symlink to the system interpreter
# (venv doesn't copy the binary), so comparing realpath()s of sys.executable
# and the venv's python3 would find them identical either way and never
# detect "not activated." What actually distinguishes an active venv is
# sys.prefix, which Python derives from pyvenv.cfg based on how it was
# invoked -- that's the check to use here, not the interpreter path.
_VENV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv")
_VENV_PYTHON = os.path.join(_VENV_DIR, "bin", "python3")
if os.path.exists(_VENV_PYTHON) and os.path.abspath(sys.prefix) != os.path.abspath(_VENV_DIR):
    os.execv(_VENV_PYTHON, [_VENV_PYTHON] + sys.argv)

import asyncio
import atexit
import collections
import datetime
import json
import math
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# pyserial backs the Pipettor tab's own RS-485 link (SOCON/SOPA ADP
# SC-STxxx, proprietary ASCII framing — see the "Pipettor protocol" section
# below). It's independent of the pymodbus connection the x/y/z motors and
# grippers use, so it's optional at import time the same way pipettor_
# control.py treated it: this app still runs fine without a pipettor
# attached, it just can't open a real serial port for that tab.
try:
    import serial
    import serial.tools.list_ports
    HAVE_PYSERIAL = True
except Exception:
    HAVE_PYSERIAL = False

# Points one level deeper than the "hardware" package root (relative to this
# file, not a hardcoded machine path — needed to run on any Pi/checkout) and
# imports motor_helpers.py directly as a top-level module rather than via
# "hardware.motor_helpers". That dotted form would run hardware/__init__.py,
# which eagerly imports the camera modules (toolcam.py/uvc_camera.py) and
# drags in opencv-python/numpy as hard requirements for this app even though
# it never touches a camera — a heavy, fragile dependency (opencv routinely
# fails to import on a fresh Pi, e.g. missing libGL) this file has no reason
# to need. motor_helpers.py itself only imports stdlib, so importing it this
# way is safe.
sys.path.insert(0, str(Path(__file__).parent / "openshelf_hardware/packages/hardware/src/hardware"))
# motor_helpers.py isn't on this machine; the only two registers it supplied are hard-coded below
# (same values zeon_cherrypicker.py uses).
from pymodbus.client import ModbusSerialClient

# ── Encoder registers ─────────────────────────────────────────────────────────
REG_ENC_POS_H = 0x602C   # "Position High (Actual)" in motor_helpers
REG_ENC_POS_L = 0x602D   # "Position Low (Actual)" in motor_helpers

# ── Configuration ─────────────────────────────────────────────────────────────
PORT = "auto"   # motor bus: "auto" = find it by probing (see _find_motor_bus), or a fixed path like "/dev/ttyUSB0"
BAUD = 115200
pause_time = 0.0

MOTORS = {
    "y": {
        # Single motor on the y axis (was two ganged motors, ids 1 and 8,
        # with every write mirrored to both). With one id, _write_mirrored
        # just writes to that motor and the multi-motor divergence/sync
        # checks (_read_encoder_mm, _sync_correct, _home_motor) skip
        # themselves since they only run when len(ids) > 1.
        # Direction flipped to match zeon_cherrypicker.py (was dr=0, home_dir=1).
        "ids": [3], "dr": 1, "home_dir": 0, "mpr": 80.0, "ppr": 10000,
        "rpm": 100, "home_rpm": 50, "home_accel": 2000, "accel": 2000, "lim": 170.0,
        "fault_bit": True,   # same drive type as x — fault bit is valid
    },
    "x": {
        # 2:1 gear reduction now between motor and leadscrew/pulley — the
        # motor has to turn twice as far for the same linear travel, so mm
        # per motor revolution is half what it was (was 80.0 direct-drive).
        # ppr (pulses/rev) is a property of the motor's own encoder, not
        # affected by external gearing, so it's unchanged.
        # Direction flipped to match zeon_cherrypicker.py (was dr=0, home_dir=1).
        "ids": [1], "dr": 1, "home_dir": 0, "mpr": 40.0, "ppr": 10000,
        "rpm": 400, "home_rpm": 100, "home_accel": 1000, "accel": 1000, "lim": 225.0,     #Soft position limit at 225 for gripper, gantry limit 225
        "fault_bit": True,
    },
    "z_gripper": {
        # Now the active vertical/insertion axis for grab/place/decap/
        # recap/test_well/move_tube/tube_loop/extend_xyz — took over that
        # role from the old placeholder "z" axis (still present, still
        # disabled, no longer referenced by any of those functions). Wired
        # through the same generic single-motor machinery as every other
        # axis (home()/extend_to()/read_positions/manual_mode/limits all
        # iterate MOTORS generically).
        #
        # TODO: mpr/ppr/rpm/home_rpm/accel/lim below are STILL copied
        # verbatim from the old "z" axis as an unverified placeholder — id
        # 4 is confirmed responding on the bus, but nothing about its real
        # lead screw/belt pitch or travel range has been confirmed.
        # Enabled at the user's explicit go-ahead despite that; double-check
        # "lim" in particular before trusting it near a real travel limit.
        #
        # dr and home_dir are both deliberately opposite y's (y: dr=1,
        # home_dir=0) — per the user, z_gripper's direction must be
        # opposite y's for both regular moves and the homing search, not
        # just one of the two. Flipped together with y to match
        # zeon_cherrypicker.py (was dr=1, home_dir=0).
        "ids": [4], "dr": 0, "home_dir": 1, "mpr": 4.0, "ppr": 10000,
        "rpm": 1400, "home_rpm": 400, "home_accel": 1000, "accel": 500, "lim": 120.0,
        "fault_bit": True,   # same drive type as x — fault bit is valid
        "enabled": True,
    },
    "z_pipettor": {
        # Same situation as z_gripper above — independent vertical actuator
        # on the pipettor head, wired through the same generic machinery,
        # real id known (30) but mechanical parameters below are an
        # unconfirmed copy of the main "z" axis's placeholder values.
        # Disabled until confirmed.
        "ids": [30], "dr": 1, "mpr": 4.0, "ppr": 10000,
        "rpm": 450, "home_rpm": 150, "home_accel": 1000, "accel": 500, "lim": 120.0,
        "fault_bit": True,   # same drive type as x — fault bit is valid
        "enabled": False,
    },
}

# Soft position limits ("lim" above) are edited live from the UI's /limits
# panel — persist whatever's set there so a restart doesn't silently revert
# to these hardcoded defaults. Loaded immediately after MOTORS so every
# later use of MOTORS[...]["lim"] already sees the saved value. Uses plain
# print() rather than _log() for its own error path since _log isn't
# defined yet this early in the module.
LIMITS_FILE = Path(__file__).parent / "limits.json"


def _load_limits() -> None:
    if not LIMITS_FILE.exists():
        return
    try:
        saved = json.loads(LIMITS_FILE.read_text())
        for name, lim in saved.items():
            if name in MOTORS and isinstance(lim, (int, float)) and lim > 0:
                MOTORS[name]["lim"] = lim
    except Exception as e:
        print(f"[limits] could not read saved limits ({e}); using defaults")


def _save_limits() -> None:
    LIMITS_FILE.write_text(json.dumps({name: cfg["lim"] for name, cfg in MOTORS.items()}, indent=2))


_load_limits()

GRIPPER = {
    "id":        1,               # Modbus slave ID of the Basegrip parallel gripper (factory default 1)
    "port":      "auto",          # its own USB adapter (CH343, shows up as /dev/ttyACM*), separate from the
                                  # motor bus. "auto" = find it by probing (see _find_basegrip), or a fixed path
    "baud":      115200,          # 8N1, per the Basegrip datasheet
    "search":    False,           # False = don't look for the static gripper at all (disabled for now).
                                  # Processes that need it (showcase decap, move tube, decap tube) refuse
                                  # to start; home all skips opening it. Set True to search again.
    "timeout_s": 5.0,             # max time to wait for open/close to settle
}

# Two independent, simultaneously-usable grippers — NOT alternate modes of
# the same gripper. GRIPPER above is "Static Gripper" (unchanged). The pair
# below is "Spinning Gripper" (TubeGrip crash-home grip + Spin rotation),
# ported from spinning_gripper.py, for rigs with both mounted at once.
GRIPPER_TUBEGRIP = {
    "id":        6,       # Modbus slave ID — matches spinning_gripper.py GRIPPERS["1"]
    "open_pos":  0,       # encoder counts — legacy/unused now that moveto uses the measured range
    "close_pos": 12000,   # encoder counts — legacy/unused now that moveto uses the measured range
    "speed_rpm": 100.0,
    "jog_rpm": 100.0,             # still much slower than speed_rpm — used for the confirmed
                                 # relative-move (0x00DE) mechanism moveto/measure rely on. Raised
                                 # from 10.0 -> 15.0 on request 2026-08-12 (moves felt slow); the
                                 # distance-scaled settle timeout in _tubegrip_moveto_percent
                                 # already has enough margin to absorb this without retuning it —
                                 # a faster real speed only means moves finish sooner, never later.
    "grab_force": 100,          # grip torque 0-100% (mapped to register level 0-230)
    "open_offset_pulses": 500,  # pulses backed off the mechanical stop after crash-home open;
                                 # raising this widens the open gap.
    "close_offset_pulses": 0,   # pulses backed off after crash-home close/grip stall; 0 means the
                                 # stall point itself becomes the new position-0 reference ("close is 0").
    "position": None,           # live encoder reading — see /state and the WS loop
    "measured_open_range": None,   # peak pulses observed during a measure-open run
    "measured_close_range": None,  # peak pulses observed during a measure-close run
    "open_direction_sign": None,   # +1/-1, which raw sign "open" is relative to a closed=0
                                    # reference — measured empirically, never assumed (see
                                    # _tubegrip_moveto_percent)
    "zero_ref": None,           # "closed"/"open"/None — which physical state position 0 currently
                                 # means, set by the last real collision-home crash (open/close/home).
                                 # Percent moves (_tubegrip_moveto_percent) require "closed".
    "open_percent": 0.0,    # what the "open" button moves to (percent of measured range, 0=open,
                             # 100=closed) — user-settable via "set as open", persisted to disk;
                             # overwritten below with any saved value from TUBEGRIP_TARGETS_FILE.
    "close_percent": 100.0,  # same as open_percent but for the "close" button.
}

GRIPPER_SPIN = {
    "id":        5,      # Modbus slave ID (DIP SW1-5)
    "open_pos":  12000,   # encoder counts — open position
    "close_pos": 0,       # encoder counts — closed position
    "cap_holder_pos": 0,  # encoder counts — angle to rotate to before approaching the cap holder
    "speed_rpm": 100.0,
    "position": None,     # live encoder reading — see /state and the WS loop
}
# Confirmed by the user physically marking the visible rotating part and
# counting pulses for one full 360° turn — 12000. Supersedes both
# rig_config.json's programmed 10000 and the driver's own subdivision
# register (0x0024, reads 4000 — that's motor-shaft microstepping before a
# ~3:1 gearing reduction, not the output pulses/rev). Used to let Spin's
# open/close positions and rotate-to-angle be set in real-world units
# instead of raw encoder counts.
GRIPPER_SPIN_COUNTS_PER_REV = 12000
GRIPPER_SPIN_POS_TOLERANCE = 30       # counts (~0.9°) — how close counts as "arrived"
GRIPPER_SPIN_START_TIMEOUT_S = 1.0    # fail if the encoder hasn't left the start by then
GRIPPER_SPIN_MOVE_TIMEOUT_S = 8.0     # overall cap, same as the old settle timeout
def _set_spin_positions(open_pos: int, close_pos: int, cap_holder_pos: int) -> None:
    """Pulse positions are the source of truth; *_rev are derived for display."""
    for name, pos in (("open", open_pos), ("close", close_pos), ("cap_holder", cap_holder_pos)):
        GRIPPER_SPIN[f"{name}_pos"] = int(pos)
        GRIPPER_SPIN[f"{name}_rev"] = int(pos) / GRIPPER_SPIN_COUNTS_PER_REV


_set_spin_positions(GRIPPER_SPIN["open_pos"], GRIPPER_SPIN["close_pos"], GRIPPER_SPIN["cap_holder_pos"])

# NA-W9308 laser distance sensor (60-140 mm range) on the motor bus. Set to
# ID 2 (factory 1 clashed with x) and 115200 baud on 2026-10-23. Distance is
# FC04 input registers 0x0000-0x0001, signed INT32 high word first, in
# 0.01 mm units (raw 12396 = 123.96 mm); 0 means nothing in range. 0x000E-
# 0x000F holds the laser's own baud rate, used to confirm it's the laser.
# Protocol: /home/cc/stepper-control/Laser Scan NA-W9101.pdf (whole W9xxx series).
LASER = {
    "id":           2,
    "reg_distance": 0x0000,
    "reg_baud":     0x000E,
    "scale_mm":     0.01,
    "range_mm":     (60.0, 140.0),
}
# The laser-to-TubeGrip-teeth offset is calibration, not config: it's set on
# the laser tab and saved as "laser_teeth_offset_mm" in calibration.json.
# It's the laser reading when a surface touches the bottom of the TubeGrip
# teeth, so reading - offset = how far that surface is below the teeth
# (negative = above them). The scan height (z_gripper mm to hold while
# scanning) is saved alongside it as "laser_scan_z_mm". "laser_stand_xy" is
# the x/y where the laser points at the pipetting stand; together with the
# gripper's calibrated "pipetting_stand" it gives the laser's x/y offset from
# the gripper (see _laser_gripper_offset).

# ── Register map ──────────────────────────────────────────────────────────────
REG_MOTOR_DIR   = 0x0007
REG_MOTOR_ENABLE = 0x000F  # "Pr0.07 Forced enable by software" — same register
                            # stepper_motor_api.py's motor_connect/motor_disconnect
                            # use (there: REG_ENABLE). CANNOT force-disable — confirmed
                            # against the Leadshine iCS-RS series manual: DI1 is a
                            # normally-closed enable input, unwired on this rig so it's
                            # permanently asserted, and Pr0.07 only ever forces *toward*
                            # enable (writing 0 just stops forcing, it doesn't disable).
                            # See REG_HOLDING_CURRENT_PCT for the real manual-mode lever.
REG_HOLDING_CURRENT_PCT = 0x0193  # Pr5.01 "Percentage of holding current", 0-50, default 50.
                                   # Since this driver has no software force-disable, manual
                                   # mode drops this to 0% instead to let the axis be moved
                                   # by hand, and restores the original value on exit.
DEFAULT_HOLDING_CURRENT_PCT = 50
REG_MOTION_ST   = 0x1003
REG_CTRL_WORD   = 0x1801
REG_CUR_ALARM   = 0x2203
REG_AUTO_HOME = 0x6000
TRIGGER       = 0x6002
REG_PR0_MODE  = 0x6200
REG_PR0_POS_H = 0x6201
REG_PR0_POS_L = 0x6202
REG_PR0_VEL   = 0x6203
REG_PR0_ACCEL = 0x6204
REG_PR0_DECEL = 0x6205

TRIG_PATH0    = 0x10
TRIG_SET_ZERO = 0x21

STATUS_COMPLETE_BIT = 5
STATUS_FAULT_BIT    = 0
ALARM_LIMIT_STOP    = 32

HOME_TIMEOUT      = 60.0
MOVE_TIMEOUT      = 60.0
MOVE_TOLERANCE_MM = 0.1

# How far a motor's position has to move (from a reference point) to count
# as "it actually moved" — used wherever "has this motor really started
# moving" needs to be checked without gating on elapsed time. Originally
# added for a ganged multi-motor axis (currently y) during homing: checked
# purely by comparing each poll against the previous one, not against
# elapsed time — a motor that never shows this much movement is still
# caught, just by falling through to the ordinary timeout like any other
# stuck-drive case, rather than a separate timed grace period. Also used by
# extend_to's has_moved tracking for the same reason — see its own comment
# for the concrete bug a time-based has_moved caused there.
MOTOR_MOVED_EPSILON_MM = 0.05

# ── Shared state ──────────────────────────────────────────────────────────────
_client: Optional[ModbusSerialClient] = None
_lock   = threading.Lock()
RETRIES = 3
# Reply timeout on the motor bus. Drives answer within a few ms at 115200, so
# this only matters for a lost/garbled reply — it was 0.5 s, which held the
# bus (and every button press queued behind it) for half a second per miss.
BUS_TIMEOUT_S = 0.15

_is_homed:  dict[str, bool] = {n: False for n in MOTORS}
_manual_mode  = False   # True while x/z/y holding current is dropped for hand-moving
_manual_mode_saved_current: dict[str, dict[int, int]] = {}  # per-axis {motor id: holding-current %} to restore on exit
# Per motor id: pulses to add to the driver's own position to get the app's
# position. Non-zero only after manual mode — leaving it re-zeroes each
# driver where the axis was left (see _rezero_in_place) so it doesn't drive
# back to its old commanded position, and this keeps the coordinates
# unchanged. Cleared when the axis is homed.
_pos_offset_pulses: dict[int, int] = {}
_ALL_MOTORS   = ["y", "x", "z_gripper", "z_pipettor"]

_connected  = False
_log_lines: collections.deque = collections.deque(maxlen=500)
_stop_flag  = threading.Event()


def _forget_homing(why: str) -> None:
    """Every axis and TubeGrip need homing again: a bus that dropped or a
    drive that lost power has lost its zero (same as zeon_cherrypicker.py)."""
    for n in _is_homed:
        _is_homed[n] = False
    for key in ("measured_open_range", "measured_close_range", "open_direction_sign", "zero_ref"):
        GRIPPER_TUBEGRIP[key] = None
    _log(f"[homing] {why} — all axes and TubeGrip need homing again")
_laser_live = False   # set once home all completes; the WS loop then streams laser readings
_op_thread: Optional[threading.Thread] = None

# ── Session tracking ──────────────────────────────────────────────────────────
SESSIONS_FILE   = Path(__file__).parent / "sessions.json"
NOTES_FILE      = Path(__file__).parent / "notes.json"
CALIBRATION_FILE = Path(__file__).parent / "calibration.json"
TUBEGRIP_TARGETS_FILE = Path(__file__).parent / "tubegrip_targets.json"
PIPETTOR_CONFIG_FILE = Path(__file__).parent / "pipettor_config.json"

# ── Plate grids (tube plates and cap holder plates) ───────────────────────────
# Each plate stores its own grid under plate["grid"]: rows are numbered
# (1, 2, 3...), columns are lettered (A, B, C...), so a well id is
# letter + number ("C7"). Pitch is the centre-to-centre spacing in mm
# between neighbouring rows / columns. Plates saved before grids existed
# have no "grid" and get the old fixed size (tube plate 12 rows x 8 columns,
# cap holder 8 rows x 6 columns) with no pitch.
#
# Positions come from the 4 calibrated corner positions by bilinear
# interpolation, exactly as before. When pitch is set, corners that haven't
# been calibrated yet are filled in from a calibrated one plus pitch, so a
# plate can be used with as few as one corner calibrated (see _plate_corners).
_PLATE_DEFAULT_GRID  = {"rows": 12, "cols": 8, "row_pitch_mm": None, "col_pitch_mm": None}
_HOLDER_DEFAULT_GRID = {"rows": 8,  "cols": 6, "row_pitch_mm": None, "col_pitch_mm": None}
_GRID_MAX_COLS = 26   # one letter per column
_GRID_MAX_ROWS = 99

def _grid(plate_data: dict, default: dict) -> dict:
    g = dict(default)
    g.update((plate_data or {}).get("grid") or {})
    return g

def _grid_letters(g: dict) -> list:
    return [chr(ord("A") + i) for i in range(int(g["cols"]))]

def _grid_positions(g: dict) -> list:
    """Every position id, column by column (A1, A2, ... then B1, ...)."""
    return [c + str(r) for c in _grid_letters(g) for r in range(1, int(g["rows"]) + 1)]

def _grid_corner_names(g: dict) -> tuple:
    """(A<last row>, <last col><last row>, A1, <last col>1) — same order as
    the old fixed ("A12", "H12", "A1", "H1")."""
    last_c = _grid_letters(g)[-1]
    last_r = int(g["rows"])
    return (f"A{last_r}", f"{last_c}{last_r}", "A1", f"{last_c}1")

def _grid_index(g: dict, pos_id: str):
    """(column index, row index), both from 0, or None if pos_id isn't on this grid."""
    if not isinstance(pos_id, str) or len(pos_id) < 2 or not pos_id[1:].isdigit():
        return None
    ci = ord(pos_id[0].upper()) - ord("A")
    ri = int(pos_id[1:]) - 1
    if not (0 <= ci < int(g["cols"]) and 0 <= ri < int(g["rows"])):
        return None
    return ci, ri

def _plate_corners(plate_data: dict, default: dict) -> dict | None:
    """The plate's 4 corners as {name: {x, z, y}}, or None if it can't be
    located yet. Calibrated corners are used as-is. Missing ones are filled
    in from the nearest calibrated corner plus pitch, when pitch is set:
    the column direction comes from two calibrated corners in the same row
    (the row direction likewise), otherwise it's taken square to the other
    direction, and with nothing to go on columns run along +x and rows
    along +y. A filled-in corner takes its z from the corner it was
    filled from."""
    g = _grid(plate_data, default)
    saved = (plate_data or {}).get("corners", {}) or {}
    cols, rows = int(g["cols"]), int(g["rows"])
    names = _grid_corner_names(g)
    idx = {names[0]: (0, rows - 1), names[1]: (cols - 1, rows - 1),
           names[2]: (0, 0),        names[3]: (cols - 1, 0)}
    known = {n: saved[n] for n in idx if n in saved}
    if len(known) == len(idx):
        return {n: known[n] for n in names}
    cp, rp = g.get("col_pitch_mm"), g.get("row_pitch_mm")
    if not known or not cp or not rp:
        return None

    def _dir(pairs):
        for a, b in pairs:
            if a in known and b in known and a != b:
                dx = known[b]["x"] - known[a]["x"]; dy = known[b]["y"] - known[a]["y"]
                n = math.hypot(dx, dy)
                if n > 1e-6:
                    return dx / n, dy / n
        return None
    u = _dir([(names[2], names[3]), (names[0], names[1])])   # along a row: A -> last column
    v = _dir([(names[2], names[0]), (names[3], names[1])])   # along a column: 1 -> last row
    if u is None and v is None:
        u, v = (1.0, 0.0), (0.0, 1.0)
    elif u is None:
        u = (v[1], -v[0])
    elif v is None:
        v = (-u[1], u[0])

    out = {}
    for n in names:
        if n in known:
            out[n] = known[n]
            continue
        ci, ri = idx[n]
        src = min(known, key=lambda k: abs(idx[k][0] - ci) + abs(idx[k][1] - ri))
        dc, dr = ci - idx[src][0], ri - idx[src][1]
        p = known[src]
        out[n] = {
            "x": round((p["x"] + dc * cp * u[0] + dr * rp * v[0]) * 100) / 100,
            "z": p["z"],
            "y": round((p["y"] + dc * cp * u[1] + dr * rp * v[1]) * 100) / 100,
        }
    return out

def _plate_pos(plate_data: dict, default: dict, pos_id: str) -> dict | None:
    """x/z/y of one position by bilinear interpolation between the plate's
    corners, or None if pos_id isn't on the plate or it can't be located yet."""
    g = _grid(plate_data, default)
    ij = _grid_index(g, pos_id)
    corners = _plate_corners(plate_data, default)
    if ij is None or corners is None:
        return None
    cols, rows = int(g["cols"]), int(g["rows"])
    s = ij[0] / (cols - 1) if cols > 1 else 0.0   # 0 = column A, 1 = last column
    t = ij[1] / (rows - 1) if rows > 1 else 0.0   # 0 = row 1, 1 = last row
    pAR, pLR, pA1, pL1 = (corners[n] for n in _grid_corner_names(g))
    def _bi(k):
        return (1-s)*(1-t)*pA1[k] + s*(1-t)*pL1[k] + (1-s)*t*pAR[k] + s*t*pLR[k]
    return {
        "x": round(_bi("x") * 10) / 10,
        "z": round(_bi("z") * 10) / 10,
        "y": round(_bi("y") * 10) / 10,
    }

def _well_pos(plate_data: dict, well_id: str) -> dict | None:
    return _plate_pos(plate_data, _PLATE_DEFAULT_GRID, well_id)

def _holder_pos(plate_data: dict, pos_id: str) -> dict | None:
    return _plate_pos(plate_data, _HOLDER_DEFAULT_GRID, pos_id)

def _next_open_holder_slot():
    """First unoccupied position across the located holder plates (plate
    insertion order, then column by column A1, A2... within a plate).
    Returns (plate_name, position_id, xyz_dict), or None if every located
    plate is full (or none can be located at all).
    """
    for plate_name, plate_data in _calibration.get("holder_plates", {}).items():
        if _plate_corners(plate_data, _HOLDER_DEFAULT_GRID) is None:
            continue
        occupied = set(plate_data.get("caps", []))
        for pos_id in _grid_positions(_grid(plate_data, _HOLDER_DEFAULT_GRID)):
            if pos_id in occupied:
                continue
            xyz = _holder_pos(plate_data, pos_id)
            if xyz is not None:
                return plate_name, pos_id, xyz
    return None

_tube_loop_state: dict = {"running": False, "status": "idle", "moves_done": 0, "total": 0}

_current_user:    Optional[str] = None
_current_machine: Optional[str] = None
_session_start:   Optional[datetime.datetime] = None
_current_ops:     list = []
_current_errors:  list = []


def _ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _log_op(command: str) -> None:
    if _current_user:
        _current_ops.append({"time": _ts(), "command": command})


def _track_error(motor: str, description: str) -> None:
    _current_errors.append({"time": _ts(), "motor": motor, "description": description})


def _load_sessions() -> list:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except Exception:
            pass
    return []


def _save_session(user: str, machine: str, start: datetime.datetime, end: datetime.datetime,
                  ops: list, errors: list) -> None:
    sessions = _load_sessions()
    duration = int((end - start).total_seconds() / 60)
    sessions.append({
        "user":         user,
        "machine":      machine,
        "login":        start.strftime("%Y-%m-%d %H:%M:%S"),
        "logout":       end.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_min": duration,
        "operations":   ops,
        "errors":       errors,
    })
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


def _flush_session_on_exit() -> None:
    if _current_user and _session_start:
        _save_session(_current_user, _current_machine or "", _session_start,
                      datetime.datetime.now(), list(_current_ops), list(_current_errors))

atexit.register(_flush_session_on_exit)


ALARM_CODES = {
    0x00: "No alarm",
    0x01: "Over current",
    0x02: "Over voltage",
    0x20: "Limit stop",
    0x40: "Current sampling fault",
    0x80: "Failed to lock shaft",
    0x100: "EEPROM fault",
    0x200: "Auto-tuning fault",
}


def _alarm_text(code: int) -> str:
    return ALARM_CODES.get(code, f"unknown (0x{code:04X})")


def _log(msg: str) -> None:
    print(msg)
    _log_lines.append(msg)


# ── Modbus helpers ────────────────────────────────────────────────────────────

# ── Serial port discovery ─────────────────────────────────────────────────────
# USB serial adapters get renumbered (ttyUSB0 <-> ttyUSB1, ttyACM0 <-> ttyACM1)
# depending on plug order, so instead of hardcoding paths each device is
# found by what answers on the port. Read-only: discovery only ever reads
# registers, never writes.
_bus_port: Optional[str] = None       # port the motor bus is open on
_bus_baud: int = BAUD
_bus_auto = False                     # True when the bus was found by discovery (the watcher may move it)
_bus_dev = None                       # _dev_id of _bus_port when it was opened
_gripper_port: Optional[str] = None   # port the Basegrip was found on (when GRIPPER["port"] is "auto")
_gripper_dev = None
PORT_WATCH_INTERVAL_S = 2.0

def _is_auto(port: Optional[str]) -> bool:
    return (port or "").strip().lower() in ("", "auto")

def _serial_candidates(prefixes: tuple = ("/dev/ttyUSB", "/dev/ttyACM")) -> list:
    """USB serial ports present right now, ttyUSB* before ttyACM*, lowest number first."""
    if HAVE_PYSERIAL:
        found = [p.device for p in serial.tools.list_ports.comports()]
    else:
        import glob
        found = glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
    def _key(dev):
        pre = next(i for i, x in enumerate(prefixes) if dev.startswith(x))
        num = dev[len(prefixes[pre]):]
        return pre, int(num) if num.isdigit() else 0
    return sorted((d for d in found if d.startswith(prefixes)), key=_key)

def _dev_id(port: Optional[str]):
    """Identity of a device node. A USB adapter that is unplugged/replugged or
    power-cycled gets a freshly created node, even under the same name, so
    a change here means the open handle is dead."""
    try:
        st = os.stat(port)
        return st.st_ino, st.st_ctime_ns
    except (OSError, TypeError):
        return None

def _port_gone(port: Optional[str], dev) -> bool:
    return port is not None and (dev is None or _dev_id(port) != dev)

def _pipettor_port_in_use() -> set:
    """Never probe the pipettor's port while its tab holds it open."""
    info = globals().get("_pipettor_info") or {}
    return {info["port"]} if info.get("connected") and info.get("port") else set()

def _ports_held_elsewhere() -> dict:
    """{port: "pid command"} for serial ports another process has open —
    e.g. zeon_cherrypicker.py. Probing those would garble that program's
    traffic, so discovery skips them (same as zeon_cherrypicker.py)."""
    import glob
    held = {}
    me = os.getpid()
    for fd_dir in glob.glob("/proc/[0-9]*/fd"):
        pid = int(fd_dir.split("/")[2])
        if pid == me:
            continue
        try:
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue
                if target.startswith(("/dev/ttyUSB", "/dev/ttyACM")) and not target.endswith(" (deleted)"):
                    try:
                        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
                    except OSError:
                        cmd = ""
                    held[target] = f"{pid} {cmd}"
        except OSError:
            continue
    return held

def _ports_to_skip(quiet: bool = True) -> set:
    """Ports discovery must never probe: the pipettor's (this app holds it)
    and any port another program has open."""
    held = _ports_held_elsewhere()
    if not quiet:
        for port, who in held.items():
            _log(f"[ports] skipping {port} — open in another program (pid {who})")
    return _pipettor_port_in_use() | set(held)

def _static_gripper_disabled() -> bool:
    """True while searching for the static gripper is switched off (GRIPPER["search"])."""
    return not GRIPPER.get("search", True) and _is_auto(GRIPPER["port"])

def _static_gripper_refused() -> Optional[str]:
    """For processes that can't run without the static gripper."""
    if _static_gripper_disabled():
        return "static gripper is disabled (GRIPPER[\"search\"] = False) — this needs it"
    return None

def _probe_port(port: str, baud: int, reads: list) -> Optional[dict]:
    """Opens port briefly and tries each (slave id, register) read in turn.
    Returns {(id, register): value} for the ones that answered, or None if
    the port couldn't be opened."""
    c = ModbusSerialClient(port=port, baudrate=baud, parity="N", stopbits=1,
                           bytesize=8, timeout=0.2, retries=0)
    try:
        if not c.connect():
            return None
        got = {}
        for sid, addr in reads:
            try:
                r = c.read_holding_registers(addr, count=1, device_id=sid)
                if not r.isError():
                    got[(sid, addr)] = r.registers[0]
            except Exception:
                pass
        return got
    except Exception:
        return None
    finally:
        c.close()

def _find_motor_bus(baud: int = BAUD, skip: set = frozenset(), quiet: bool = False) -> Optional[str]:
    """First port where any of the bus's own devices answers: Spin and
    TubeGrip (position register) or an enabled axis motor (encoder). ID
    GRIPPER["id"] (1) is left out of the probe so the Basegrip, which is
    also ID 1, can't be mistaken for the bus — the bus always has other
    IDs on it."""
    reads = [(GRIPPER_SPIN["id"], 0x0004), (GRIPPER_TUBEGRIP["id"], 0x0004)]
    for cfg in MOTORS.values():
        if cfg.get("enabled", True):
            reads += [(sid, REG_ENC_POS_H) for sid in cfg["ids"] if sid != GRIPPER["id"]]
    tried = []
    for port in _serial_candidates():
        if port in skip:
            continue
        tried.append(port)
        got = _probe_port(port, baud, reads)
        if got:
            ids = sorted({sid for sid, _ in got})
            _log(f"[bus] found motor bus on {port} (answering IDs {ids})")
            return port
    if not quiet:
        _log(f"[bus] motor bus not found — tried {tried or 'no USB serial ports'}")
    return None

def _find_basegrip(skip: set = frozenset(), quiet: bool = False) -> Optional[str]:
    """First /dev/ttyACM* port (the Basegrip's CH343 adapter shows up as
    ACM; the motor bus's FTDI as ttyUSB) where ID GRIPPER["id"] answers with
    a valid gripper status and control mode. Kept to ACM ports so the x
    axis, which is also ID 1, can never be picked up as the gripper."""
    sid = GRIPPER["id"]
    reads = [(sid, GRIPPER_REG_STATUS), (sid, GRIPPER_REG_CONTROL_MODE)]
    tried = []
    for port in _serial_candidates(("/dev/ttyACM",)):
        if port in skip:
            continue
        tried.append(port)
        got = _probe_port(port, GRIPPER["baud"], reads) or {}
        if (got.get((sid, GRIPPER_REG_STATUS)) in GRIPPER_STATUS_TEXT
                and got.get((sid, GRIPPER_REG_CONTROL_MODE)) in (0, 1)):
            _log(f"  [gripper] found Basegrip on {port}")
            return port
    if not quiet:
        _log(f"  [gripper] Basegrip not found — tried {tried or 'no /dev/ttyACM* ports'}")
    return None


def _do_connect(port: str = PORT, baud: int = BAUD) -> bool:
    global _client, _connected, _bus_port, _bus_baud, _bus_dev
    try:
        if _client:
            _client.close()
        _client = ModbusSerialClient(
            port=port, baudrate=baud, parity="N", stopbits=1, bytesize=8,
            timeout=BUS_TIMEOUT_S, retries=0,
        )
        _connected = _client.connect()
        _bus_port = port if _connected else None
        _bus_baud = baud
        _bus_dev = _dev_id(port) if _connected else None
        _log(f"[bus] {'connected' if _connected else 'FAILED'} — {port} @ {baud}")
        if _connected:
            _forget_homing("motor bus connected")
        return _connected
    except Exception as e:
        _log(f"[bus] error: {e}")
        _connected = False
        return False


def _read(sid: int, addr: int, count: int = 1) -> list[int] | None:
    if not _client:
        return None
    for attempt in range(RETRIES):
        try:
            with _lock:
                r = _client.read_holding_registers(addr, count=count, device_id=sid)
                if not r.isError():
                    return r.registers
        except Exception:
            pass
        if attempt < RETRIES - 1:
            time.sleep(0.05)
    return None


def _read_once(sid: int, addr: int, count: int = 1) -> list[int] | None:
    """Single try, for the live readout — a motor that doesn't answer
    shouldn't hold the bus for RETRIES timeouts on every refresh, delaying
    whatever button was just pressed."""
    if not _client:
        return None
    try:
        with _lock:
            r = _client.read_holding_registers(addr, count=count, device_id=sid)
            return None if r.isError() else r.registers
    except Exception:
        return None


def _read_int32(sid: int, addr: int, once: bool = False) -> Optional[int]:
    """Signed INT32 read, low word first (matches _write_int32's ordering)."""
    regs = (_read_once if once else _read)(sid, addr, 2)
    if regs is None:
        return None
    u = regs[0] | (regs[1] << 16)
    return u - 0x1_0000_0000 if u >= 0x8000_0000 else u


def _write(sid: int, addr: int, val: int) -> bool:
    if not _client:
        return False
    for attempt in range(RETRIES):
        try:
            with _lock:
                r = _client.write_register(addr, val, device_id=sid)
                if not r.isError():
                    return True
        except Exception:
            pass
        if attempt < RETRIES - 1:
            time.sleep(0.05)
    return False


def _write_int32(sid: int, addr: int, value: int) -> bool:
    if not _client:
        return False
    u = value & 0xFFFF_FFFF
    lo, hi = u & 0xFFFF, (u >> 16) & 0xFFFF
    for attempt in range(RETRIES):
        try:
            with _lock:
                r = _client.write_registers(addr, [lo, hi], device_id=sid)
                if not r.isError():
                    return True
        except Exception:
            pass
        if attempt < RETRIES - 1:
            time.sleep(0.05)
    return False


# ── Gripper ───────────────────────────────────────────────────────────────────

GRIPPER_REG_ENABLE       = 0x00D4
GRIPPER_REG_TARGET_SPEED = 0x00D8
GRIPPER_REG_ABS_MOVE_IMM = 0x00E8


# Static gripper — Basegrip electric parallel gripper. Open/close ported from
# Basegrip-v1.0.0/gripper_controller.py (release()/grip(), as used by
# gripper_testing.py): write 1 to the release (open) or grip (close) command
# register, then poll the status register until the motion settles. The
# gripper only accepts serial commands while its control mode register is 1
# (serial) — it powers up in IO mode (0), so this switches it over first.
#
# It sits on its own serial port (GRIPPER["port"]), not the motor bus, so it
# has its own client and lock, opened on first use. Replaces the earlier
# servo.py-style driver (control word 0x03E8 / status 0x07D0, slave 9), which
# never answered on this rig.
GRIPPER_REG_CONTROL_MODE = 0x0035  # 0 = IO control, 1 = serial
GRIPPER_REG_OPEN_CMD     = 0x0036  # write 1 -> release (open)
GRIPPER_REG_CLOSE_CMD    = 0x0037  # write 1 -> grip (close)
GRIPPER_REG_STATUS       = 0x0040  # see GRIPPER_STATUS_TEXT
GRIPPER_CMD_OPEN  = GRIPPER_REG_OPEN_CMD
GRIPPER_CMD_CLOSE = GRIPPER_REG_CLOSE_CMD
GRIPPER_STATUS_TEXT = {
    0: "idle",
    1: "gripped — object detected",
    2: "gripped — no object",
    4: "gripping",
    5: "releasing",
    6: "released — open",
}
GRIPPER_SETTLED = {0, 1, 2, 6}

_gripper_client: Optional[ModbusSerialClient] = None
_gripper_lock = threading.Lock()


def _gripper_connect(quiet: bool = False) -> bool:
    global _gripper_client, _gripper_port, _gripper_dev
    if _gripper_client is not None and _gripper_client.connected:
        return True
    if _static_gripper_disabled():
        return False
    auto = _is_auto(GRIPPER["port"])
    if auto and _gripper_port is None:
        _gripper_port = _find_basegrip(skip={_bus_port} | _ports_to_skip(), quiet=quiet)
        if _gripper_port is None:
            return False
    port = _gripper_port if auto else GRIPPER["port"]
    try:
        _gripper_client = ModbusSerialClient(
            port=port, baudrate=GRIPPER["baud"], parity="N", stopbits=1,
            bytesize=8, timeout=0.3, retries=0,
        )
        ok = _gripper_client.connect()
    except Exception as e:
        _log(f"  [gripper] port error: {e}")
        ok = False
    if not ok:
        _gripper_client = None
        if auto:
            _gripper_port = None   # unplugged or renumbered — search again next time
    _gripper_dev = _dev_id(port) if ok else None
    _log(f"  [gripper] {'connected' if ok else 'FAILED to open'} {port} @ {GRIPPER['baud']}")
    return ok


def _gripper_disconnect() -> None:
    global _gripper_client, _gripper_port
    with _gripper_lock:
        if _gripper_client is not None:
            _gripper_client.close()
            _gripper_client = None
        _gripper_port = None


def _gripper_read(addr: int) -> Optional[int]:
    with _gripper_lock:
        if not _gripper_connect():
            return None
        for attempt in range(RETRIES):
            try:
                r = _gripper_client.read_holding_registers(addr, count=1, device_id=GRIPPER["id"])
                if not r.isError():
                    return r.registers[0]
            except Exception:
                pass
            if attempt < RETRIES - 1:
                time.sleep(0.05)
    return None


def _gripper_write(addr: int, val: int) -> bool:
    with _gripper_lock:
        if not _gripper_connect():
            return False
        for attempt in range(RETRIES):
            try:
                r = _gripper_client.write_register(addr, val, device_id=GRIPPER["id"])
                if not r.isError():
                    return True
            except Exception:
                pass
            if attempt < RETRIES - 1:
                time.sleep(0.05)
    return False


def _gripper_ensure_serial_mode() -> bool:
    if _gripper_read(GRIPPER_REG_CONTROL_MODE) == 1:
        return True
    _log("  [gripper] switching control mode to serial")
    # The device acknowledges writes even when they don't take, so read back.
    return _gripper_write(GRIPPER_REG_CONTROL_MODE, 1) and _gripper_read(GRIPPER_REG_CONTROL_MODE) == 1


def _gripper_move(cmd: int) -> bool:
    """Open (GRIPPER_CMD_OPEN) or close (GRIPPER_CMD_CLOSE) the static
    gripper and wait for it to settle. Returns True once the status register
    reports a settled state; a close counts as done whether or not an object
    was detected (status 1 or 2)."""
    label = "open" if cmd == GRIPPER_CMD_OPEN else "close"
    if not _gripper_ensure_serial_mode():
        _log(f"  [gripper] {label}: could not switch to serial mode")
        return False
    t0 = time.monotonic()
    if not _gripper_write(cmd, 1):
        _log(f"  [gripper] {label}: command not acknowledged")
        return False
    while time.monotonic() - t0 < GRIPPER["timeout_s"]:
        code = _gripper_read(GRIPPER_REG_STATUS)
        # Ignore a settled status in the first 0.3 s — it can still be the
        # previous move's result before the new command is picked up.
        if code in GRIPPER_SETTLED and time.monotonic() - t0 > 0.3:
            return True
        time.sleep(0.05)
    _log(f"  [gripper] {label}: timed out after {GRIPPER['timeout_s']:.0f}s")
    return False


def _gripper_servo_diag() -> str:
    """Read-only: the gripper's status and control mode."""
    code = _gripper_read(GRIPPER_REG_STATUS)
    if code is None:
        return "status read failed"
    mode = _gripper_read(GRIPPER_REG_CONTROL_MODE)
    return (f"status={code} ({GRIPPER_STATUS_TEXT.get(code, 'unknown')}), "
            f"control_mode={'serial' if mode == 1 else 'IO' if mode == 0 else mode}")


# ── Spinning gripper (TubeGrip + Spin) ────────────────────────────────────────
# Ported from spinning_gripper.py — a second, independent gripper, always
# available alongside the static one above (not a mode switch between them).
# Confirmed on hardware there: the absolute position triggers above
# (0x00D0/0x00E8) don't reliably move the TubeGrip driver, so its open/close
# use collision-home (mode=1 — drive until stall, then back off an offset and
# mark that as the new position-0) instead. The Spin rotation motor uses the
# exact same mechanism as the static gripper (_gripper_move), just a second
# slave ID and config, so _spin_move below just mirrors _gripper_move for
# GRIPPER_SPIN.
#
# TubeGrip's intermediate positioning (moveto/measure) uses a THIRD
# mechanism instead of either of the above: the relative-move register
# (0x00DE), confirmed reliable on hardware 2026-08-12 once two bugs were
# fixed — a stray soft position-limit register (see
# _tubegrip_clear_soft_pos_limit) and a workaround write to 0x00D0 that
# turned out to itself trigger real motion (see _tubegrip_move_relative).
# See project_gripper_hardware_notes memory for the full history.

GRIPPER_REG_TORQUE_MODE  = 0x009E  # bits15~8 = mode, bits7~0 = torque level (0-230)
GRIPPER_REG_RUN_STOP     = 0x00CB
GRIPPER_TORQUE_MODE_HOME = 0x01
GRIPPER_CMD_STOP         = 0x8000
GRIPPER_REG_REL_MOVE_IMM = 0x00DE
GRIPPER_REG_SOFT_NEG_LIMIT = 0x006E  # INT32, "记忆" — default -2147483648 (effectively unlimited)
GRIPPER_REG_SOFT_POS_LIMIT = 0x0070  # INT32, "记忆" — default  2147483647 (effectively unlimited)


def _spin_move(pos: int) -> bool:
    """Used to fire the move command and return immediately — the caller
    had no way to know the physical move had actually finished, which
    caused steps chained right after a Spin move (e.g. /decap) to fire
    before Spin had actually finished rotating. Now waits for position to
    settle before returning, same polling this file already uses for
    TubeGrip (_tubegrip_observe_until_settled) — Spin is the same
    IC42ET05-family position register (0x0004) under a different slave ID.

    Settling alone isn't proof the move happened: a driver that accepts the
    command but never runs (seen on hardware 2026-10-16 — every move register
    acked, run state stayed idle) reads as "settled" immediately at its old
    position, so this used to report success for moves that never happened.
    Now only succeeds once the encoder is within GRIPPER_SPIN_POS_TOLERANCE of
    the target, and fails fast if the motor never starts moving at all.
    """
    sid = GRIPPER_SPIN["id"]
    start = _read_int32(sid, 0x0004)
    if start is None:
        _log("  [spin] move: position read failed")
        return False
    if abs(start - pos) <= GRIPPER_SPIN_POS_TOLERANCE:
        return True
    _write(sid, GRIPPER_REG_ENABLE, 0x0000)
    _write_int32(sid, GRIPPER_REG_TARGET_SPEED, int(GRIPPER_SPIN["speed_rpm"] * 100))
    if not _write_int32(sid, GRIPPER_REG_ABS_MOVE_IMM, pos):
        _log("  [spin] move: command write failed")
        return False

    t0 = time.perf_counter()
    last = start
    stable = 0
    while time.perf_counter() - t0 < GRIPPER_SPIN_MOVE_TIMEOUT_S:
        time.sleep(0.05)
        cur = _read_int32(sid, 0x0004)
        if cur is None:
            continue
        stable = stable + 1 if cur == last else 0
        last = cur
        if abs(cur - pos) <= GRIPPER_SPIN_POS_TOLERANCE:
            if stable >= 3:
                return True
            continue
        if abs(cur - start) <= GRIPPER_SPIN_POS_TOLERANCE:
            if time.perf_counter() - t0 > GRIPPER_SPIN_START_TIMEOUT_S:
                _log(f"  [spin] move to {pos}: motor never started (still at {cur}) — "
                     f"{_tubegrip_diag(sid)}")
                return False
        elif stable >= 10:
            _log(f"  [spin] move to {pos}: stopped short at {cur} — {_tubegrip_diag(sid)}")
            return False
    _log(f"  [spin] move to {pos}: timed out at {last} — {_tubegrip_diag(sid)}")
    return False


def _tubegrip_clear_soft_pos_limit() -> tuple[bool, str]:
    """Widens the positive software position limit (0x0070) back to the
    documented factory default (2147483647 — effectively unlimited).
    Read-verifies the write actually took. Doesn't touch collision-home
    (open/close) at all — this only gates position-mode moves
    (0x00D0/0x00E8/0x00DE), which is why a stray tight value here
    (confirmed on hardware: 5) silently clamped moveto/measure almost
    immediately while open/close kept working fine.
    """
    sid = GRIPPER_TUBEGRIP["id"]
    if not _write_int32(sid, GRIPPER_REG_SOFT_POS_LIMIT, 2147483647):
        return False, "write failed"
    value = _read_int32(sid, GRIPPER_REG_SOFT_POS_LIMIT)
    if value != 2147483647:
        return False, f"write did not take — read back {value}"
    return True, ""


def _tubegrip_move_relative(delta_pulses: int, rpm: Optional[float] = None) -> bool:
    """Writes a signed relative pulse count to 0x00DE — confirmed reliable
    on hardware (spinning_gripper.py, 2026-08-12), unlike the plain
    absolute-position trigger (0x00E8/0x00D0) open/close deliberately avoid.

    Pre-writes the CURRENT position (not a fixed constant) to 0x00D0 first:
    a driver quirk silently ignores the run command when both the encoder
    position and 0x00D0 are 0, but 0x00D0 is itself a write-triggers-move
    register (see PDF section 2.3, "绝对运行") — writing a fixed constant
    there previously caused a real, silent, wrong-direction move that ate
    pulses off every relative move whenever current position wasn't 0
    (confirmed: a +50 request from position 6 only netted +45, exactly the
    -5 the bogus move to position 1 introduced). Writing the current
    position instead makes that trigger zero-distance so it can't interfere.
    """
    sid = GRIPPER_TUBEGRIP["id"]
    rpm = rpm if rpm is not None else GRIPPER_TUBEGRIP.get("speed_rpm", 100.0)
    if not _write(sid, GRIPPER_REG_ENABLE, 0x0000):
        return False
    pos = _read_int32(sid, 0x0004)
    _write_int32(sid, 0x00D0, pos if pos else 1)  # workaround, see docstring
    if not _write_int32(sid, GRIPPER_REG_TARGET_SPEED, int(rpm * 100)):
        return False
    return _write_int32(sid, GRIPPER_REG_REL_MOVE_IMM, int(delta_pulses))


def _tubegrip_collision_home(cmd: int, torque_level: int) -> bool:
    """Set collision-home mode + torque limit, then fire the run/stop command
    (built by _tubegrip_collision_cmd). Includes the 20ms settle delay
    between the mode write and the trigger — confirmed necessary on hardware.
    """
    sid = GRIPPER_TUBEGRIP["id"]
    if not _write(sid, GRIPPER_REG_ENABLE, 0x0000):
        return False
    mode_val = (GRIPPER_TORQUE_MODE_HOME << 8) | (torque_level & 0xFF)
    if not _write(sid, GRIPPER_REG_TORQUE_MODE, mode_val):
        return False
    time.sleep(0.02)
    return _write(sid, GRIPPER_REG_RUN_STOP, cmd)


def _tubegrip_collision_cmd(direction: int, offset_pulses) -> int:
    """Build a collision-home run/stop command word: crash until stall, then
    back off offset_pulses and mark that point as the new position-0
    reference. offset_pulses=0 means the stall point itself becomes 0.
    """
    offset_pulses = max(0, min(0x3FFF, int(offset_pulses)))
    return ((direction & 1) << 15) | (offset_pulses << 1) | 0x0001


def _tubegrip_open(offset_pulses: Optional[float] = None) -> bool:
    """Crashes into the mechanical open stop at max torque (230), then backs
    off offset_pulses (GRIPPER_TUBEGRIP["open_offset_pulses"] by default).
    Direction bit 0 is the direction confirmed to open the jaws on this unit.
    """
    offset = offset_pulses if offset_pulses is not None else GRIPPER_TUBEGRIP["open_offset_pulses"]
    return _tubegrip_collision_home(_tubegrip_collision_cmd(0, offset), 230)


def _tubegrip_grab(torque_pct: Optional[float] = None) -> bool:
    """Drives until it stalls against the tube or the mechanical closed stop,
    then marks the stall point as position 0 (close_offset_pulses=0 by
    default — "close is 0"). Direction bit 1 is the direction confirmed to
    close/grip on this unit.
    """
    pct = torque_pct if torque_pct is not None else GRIPPER_TUBEGRIP["grab_force"]
    level = max(0, min(230, int(pct) * 230 // 100))
    close_offset = GRIPPER_TUBEGRIP["close_offset_pulses"]
    return _tubegrip_collision_home(_tubegrip_collision_cmd(1, close_offset), level)


def _tubegrip_set_enabled(enabled: bool) -> bool:
    """Enable (hold torque) or disable (release windings so it can be moved
    by hand) via register 0x00D4. Also stops any in-flight collision-home
    run first when disabling, so it doesn't resume mid-motion on re-enable.
    """
    sid = GRIPPER_TUBEGRIP["id"]
    if not enabled:
        _write(sid, GRIPPER_REG_RUN_STOP, GRIPPER_CMD_STOP)
    return _write(sid, GRIPPER_REG_ENABLE, 0x0000 if enabled else 0x0001)


GRIPPER_ALARM_NAMES = {
    0: "normal", 1: "motor phase overcurrent", 2: "supply overvoltage",
    3: "supply undervoltage", 4: "motor phase A open", 5: "motor phase B open",
    6: "other alarm / position deviation", 7: "internal 24V offset",
    8: "AI voltage error", 9: "BI voltage error", 10: "encoder error",
}
GRIPPER_RUN_STATE_NAMES = {0: "idle", 1: "about to run", 3: "running"}


def _tubegrip_clear_alarm() -> tuple[bool, str]:
    """Report the current alarm state — does NOT write register 0x00A4,
    which destructively rotates the alarm history word on this driver family
    rather than clearing anything useful.
    """
    regs = _read(GRIPPER_TUBEGRIP["id"], 0x00A3, 1)
    if regs is None:
        return False, "alarm read failed"
    code = regs[0] & 0x0F
    return True, GRIPPER_ALARM_NAMES.get(code, str(code))


TUBEGRIP_RESTART_WAIT_S = 5.0   # how long to wait for the driver to answer again after a restart


def _tubegrip_alarm_code() -> Optional[int]:
    """Current alarm (low nibble of 0x00A3; 0 = normal), or None if no reply."""
    regs = _read(GRIPPER_TUBEGRIP["id"], 0x00A3, 1)
    return None if regs is None else regs[0] & 0x0F


def _tubegrip_restart_driver() -> bool:
    """Software restart of TubeGrip's driver (0x00D4 = 0x0100) — the same
    as power-cycling it, which is what clears a latched alarm such as
    "position deviation". The driver reboots without replying, so the
    command is sent exactly once (the normal _write would retry it, i.e.
    restart it again), then this waits for it to answer, re-enables holding
    torque and returns whether the alarm is gone.

    The restart can reset the encoder count, so the measured open/close
    reference is discarded — open/close/moveto then ask for a home, and
    _tubegrip_home_sequence measures it again.
    """
    sid = GRIPPER_TUBEGRIP["id"]
    with _lock:
        try:
            _client.write_register(GRIPPER_REG_ENABLE, 0x0100, device_id=sid)
        except Exception:
            pass   # no reply is expected
    for key in ("measured_open_range", "measured_close_range", "open_direction_sign", "zero_ref"):
        GRIPPER_TUBEGRIP[key] = None
    t0 = time.monotonic()
    time.sleep(0.5)
    code = None
    while time.monotonic() - t0 < TUBEGRIP_RESTART_WAIT_S:
        code = _tubegrip_alarm_code()
        if code is not None:
            break
        time.sleep(0.2)
    if code is None:
        _log(f"  [tubegrip] driver did not answer within {TUBEGRIP_RESTART_WAIT_S:.0f}s of the restart")
        return False
    _write(sid, GRIPPER_REG_ENABLE, 0x0000)
    _log(f"  [tubegrip] driver restarted (back after {time.monotonic() - t0:.1f}s) — "
         f"alarm: {GRIPPER_ALARM_NAMES.get(code, code)}")
    return code == 0


def _tubegrip_reset_if_alarmed(label: str) -> bool:
    """Checks TubeGrip's alarm and, if one is latched, restarts its driver
    to clear it. True when TubeGrip ends up answering with no alarm."""
    code = _tubegrip_alarm_code()
    if code is None:
        _log(f"  [{label}] TubeGrip not responding — check its power and wiring")
        return False
    if code == 0:
        return True
    _log(f"  [{label}] TubeGrip alarm: {GRIPPER_ALARM_NAMES.get(code, code)} — restarting its driver to clear it")
    if _tubegrip_restart_driver():
        return True
    _log(f"  [{label}] driver restart didn't clear TubeGrip — power-cycle it")
    return False


def _tubegrip_run_state(sid: int) -> Optional[int]:
    """TubeGrip's run state from status 0x0006 bits 8-9 (see
    GRIPPER_RUN_STATE_NAMES), or None if it didn't answer."""
    regs = _read(sid, 0x0006, 2)
    return None if regs is None else (regs[0] >> 8) & 0x3


def _tubegrip_is_stuck(sid: int) -> bool:
    """True when the driver still reports "running" (GRIPPER_RUN_STATE_NAMES
    3) although the jaws have stopped — a move blocked partway, pushing
    against something. Seen on hardware 2026-10-28 after a stop: run=running,
    in_position=0, pos_err_warn=1, ~2.6 A, no alarm, position frozen. In
    that state the driver ignores every new move or collision-home command,
    and position-unchanged settle checks pass straight away, so open/grab/
    home all "succeed" without moving. Only call it once motion should have
    ended."""
    first = _read_int32(sid, 0x0004)
    if _tubegrip_run_state(sid) != 3:
        return False
    time.sleep(0.15)
    return first is not None and _read_int32(sid, 0x0004) == first and _tubegrip_run_state(sid) == 3


def _tubegrip_clear_stuck_move(label: str) -> bool:
    """If TubeGrip is stuck mid-move (_tubegrip_is_stuck), cancels it the
    way the "release TubeGrip" button does — stop command, release torque,
    re-enable — which cleared it on hardware without a power cycle. Drops
    anything in the jaws, so only for when TubeGrip is about to open or
    home anyway. Returns True if it was stuck (and cleared)."""
    sid = GRIPPER_TUBEGRIP["id"]
    if not _tubegrip_is_stuck(sid):
        return False
    _log(f"  [{label}] TubeGrip stuck mid-move ({_tubegrip_diag(sid)}) — stopping and re-enabling it")
    _tubegrip_release_push(label)
    return True


def _tubegrip_release_push(label: str) -> None:
    """Cancels whatever TubeGrip's driver is still trying to do — stop
    command, release torque, re-enable (what the "release TubeGrip" button
    does, confirmed on hardware to clear a stuck move) — then waits for the
    driver to report idle again before anything else is sent. Without the
    wait, the next command (e.g. the home right after a clear) could arrive
    before the driver is ready and be ignored. Drops anything in the jaws."""
    sid = GRIPPER_TUBEGRIP["id"]
    _tubegrip_set_enabled(False)
    time.sleep(0.1)
    _tubegrip_set_enabled(True)
    t0 = time.monotonic()
    while _tubegrip_run_state(sid) not in (0, None) and time.monotonic() - t0 < 2.0:
        time.sleep(0.05)
    time.sleep(0.3)
    # Position 0 no longer means anything known after this.
    GRIPPER_TUBEGRIP["zero_ref"] = None
    if _tubegrip_run_state(sid) not in (0, None):
        _log(f"  [{label}] TubeGrip still not idle after release ({_tubegrip_diag(sid)})")


def _tubegrip_diag(sid: int) -> str:
    """Read back alarm + run/enable status (0x00A3, 0x0006~0x0007) for
    troubleshooting when a write reports success but the motor doesn't move.
    Read-only — never changes gripper behavior.
    """
    parts = []
    regs = _read(sid, 0x00A3, 1)
    if regs is not None:
        parts.append(f"alarm={GRIPPER_ALARM_NAMES.get(regs[0] & 0x0F, regs[0] & 0x0F)}")
    else:
        parts.append("alarm read failed")
    regs = _read(sid, 0x0006, 2)
    if regs is not None:
        status = regs[0] | (regs[1] << 16)
        run_state = (status >> 8) & 0x3
        parts.append(f"run={GRIPPER_RUN_STATE_NAMES.get(run_state, run_state)}")
        parts.append(f"enable_hi={(status >> 16) & 1}")
        # BIT10 and BIT20 are two distinct manual fields both loosely named
        # "位置超差警告": BIT10 is the actual threshold-exceeded warning flag,
        # BIT20 is just the deviation's sign (0=negative, 1=positive) — this
        # was previously mislabeled as pos_err_warn=BIT20, fixed to match
        # spinning_gripper.py.
        parts.append(f"pos_err_warn={(status >> 10) & 1}")
        parts.append(f"pos_err_dir={'+' if (status >> 20) & 1 else '-'}")
        parts.append(f"at_origin={(status >> 15) & 1}")
        parts.append(f"soft_neg_limit={(status >> 13) & 1}")
        parts.append(f"soft_pos_limit={(status >> 14) & 1}")
        parts.append(f"in_position={(status >> 12) & 1}")
    else:
        parts.append("status read failed")
    pos = _read_int32(sid, 0x0004)
    parts.append(f"pos={pos}" if pos is not None else "pos read failed")
    regs = _read(sid, 0x001A, 1)
    parts.append(f"current_mA={regs[0]}" if regs is not None else "current read failed")
    neg_limit = _read_int32(sid, GRIPPER_REG_SOFT_NEG_LIMIT)
    parts.append(f"soft_neg_limit_val={neg_limit}" if neg_limit is not None else "neg limit read failed")
    pos_limit = _read_int32(sid, GRIPPER_REG_SOFT_POS_LIMIT)
    parts.append(f"soft_pos_limit_val={pos_limit}" if pos_limit is not None else "pos limit read failed")
    return ", ".join(parts)


def _tubegrip_observe_until_settled(sid: int, timeout_s: float = 3.0) -> tuple[Optional[int], bool]:
    """Polls position until it stops changing (3 consecutive equal reads) or
    timeout_s elapses. Returns (final_position, settled). Read-only — used
    by _tubegrip_moveto_percent to detect whether the relative-move register
    genuinely self-terminated.
    """
    last = None
    stable = 0
    final = None
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout_s:
        time.sleep(0.05)
        pos = _read_int32(sid, 0x0004)
        if pos is None:
            continue
        final = pos
        if pos == last:
            stable += 1
            if stable >= 3:
                return final, True
        else:
            stable = 0
        last = pos
    return final, False


def _tubegrip_observe_peak_position(sid: int, timeout_s: float = 8.0) -> tuple[int, int]:
    """Passively polls position until it stops changing (3 consecutive equal
    reads) or timeout_s elapses, returning (peak_magnitude, signed_value_at_peak)
    — the largest |position| seen, and its actual sign. The sign is how
    _tubegrip_moveto_percent learns which raw-encoder direction "open"
    really is relative to a closed=0 reference, instead of guessing.

    This NEVER tries to intervene in the motion — it only reads, never
    writes — so it carries none of the risk a jog-and-cut-power approach
    would (that raced against ~500ms-per-transaction latency and lost,
    crashing the gripper — see project_gripper_hardware_notes memory). Here
    the motion is left to run to completion exactly as it already safely
    does; this just measures how far it went.
    """
    peak = 0
    peak_signed = 0
    last = None
    stable = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout_s:
        time.sleep(0.05)
        pos = _read_int32(sid, 0x0004)
        if pos is None:
            continue
        if abs(pos) > peak:
            peak = abs(pos)
            peak_signed = pos
        if pos == last:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        last = pos
    return peak, peak_signed


def _tubegrip_measure_open() -> tuple[Optional[int], Optional[int], str]:
    """Fires the normal, already-safe open (_tubegrip_open) and passively
    observes the peak position reached during the motion. Use this (instead
    of the regular open endpoint) when you want to measure the physical
    stroke length in pulses, e.g. after a full close (position ~0) to find
    the full open-side travel. Returns (peak_magnitude, signed_peak, reason)
    — signed_peak is only a meaningful "open direction relative to
    closed=0" if this was called right after a close (zero_ref=="closed");
    callers decide whether to trust it for that.
    """
    if not _tubegrip_open():
        return None, None, "open command failed"
    peak, peak_signed = _tubegrip_observe_peak_position(GRIPPER_TUBEGRIP["id"])
    return peak, peak_signed, ""


def _tubegrip_measure_close() -> tuple[Optional[int], Optional[int], str]:
    """Same as _tubegrip_measure_open but for close (_tubegrip_grab)."""
    if not _tubegrip_grab():
        return None, None, "close command failed"
    peak, peak_signed = _tubegrip_observe_peak_position(GRIPPER_TUBEGRIP["id"])
    return peak, peak_signed, ""


def _tubegrip_measure_moved(measure) -> tuple[Optional[int], Optional[int], str]:
    """Runs _tubegrip_measure_open/_tubegrip_measure_close and fails it if
    the jaws never moved — the peak observer otherwise records the frozen
    position as a "measured" range (seen on hardware: a stuck TubeGrip
    homed "successfully" with close=open=3619 without moving at all)."""
    sid = GRIPPER_TUBEGRIP["id"]
    start = _read_int32(sid, 0x0004)
    peak, peak_signed, reason = measure()
    if peak is None or start is None:
        return peak, peak_signed, reason
    end = _read_int32(sid, 0x0004)
    if end is not None and abs(end - start) <= 200 and abs(peak - abs(start)) <= 200:
        return None, None, f"jaws did not move (position stayed at {start}; {_tubegrip_diag(sid)})"
    return peak, peak_signed, reason


def _tubegrip_moveto_percent(percent: float, rpm: Optional[float] = None) -> tuple[bool, str, dict]:
    """Moves TubeGrip to an intermediate jaw width via the confirmed-working
    relative-move register (0x00DE, see _tubegrip_move_relative), as a
    percentage of the REAL measured open-side stroke (0=open, 100=closed —
    same convention as the static gripper/Spin's moveto). TubeGrip has no
    fixed open_pos/close_pos to target the way they do (its collision-home
    actions don't drive to a fixed encoder position) — the real range only
    exists once measured.

    Requires open_direction_sign to be known — which raw-encoder sign is
    "open" relative to a closed=0 reference, empirically measured by
    _tubegrip_measure_open right after a real close (never assumed) — which
    only ever happens via /gripper/tubegrip/home, so that's required at
    least once (this also guarantees measured_open_range is current for the
    same physical configuration: grab_force/open_offset_pulses).

    Does NOT require zero_ref == "closed" at call time, only that it's
    known at all: /gripper/tubegrip/home itself ends at zero_ref == "open"
    (its last step is a measured open crash), and open/close/moveto no
    longer crash on their own, so nothing would ever bring zero_ref back to
    "closed" without this. When the reference has drifted to "open" (or
    hasn't been resolved this call), this re-establishes it with a real
    close crash (_tubegrip_measure_close — the same safe, already-validated
    action open/close always used before percent-based moves existed)
    rather than extrapolate an unconfirmed formula for an "open" reference
    frame — that extrapolation would rely on an unverified assumption
    (that the crash's re-zero is a pure coordinate shift) which is exactly
    the kind of untested motion-direction assumption that caused a prior
    crash on this hardware (see project_gripper_hardware_notes memory).
    Costs one real crash only when the reference needs re-establishing, not
    on every call.
    """
    open_range = GRIPPER_TUBEGRIP.get("measured_open_range")
    sign = GRIPPER_TUBEGRIP.get("open_direction_sign")
    if open_range is None or sign is None:
        return False, "open-side range/direction not measured yet (run /gripper/tubegrip/home first)", {}
    if GRIPPER_TUBEGRIP.get("zero_ref") != "closed":
        close_peak, _close_signed, close_reason = _tubegrip_measure_close()
        if close_peak is None:
            return False, f"could not re-establish closed reference: {close_reason}", {}
        GRIPPER_TUBEGRIP["measured_close_range"] = close_peak
        GRIPPER_TUBEGRIP["zero_ref"] = "closed"
    sid = GRIPPER_TUBEGRIP["id"]
    pct = max(0.0, min(100.0, percent))
    target = int(sign * open_range * (1.0 - pct / 100.0))
    start = _read_int32(sid, 0x0004)
    if start is None:
        return False, "could not read current position", {}
    delta = target - start
    rpm = rpm if rpm is not None else GRIPPER_TUBEGRIP.get("jog_rpm", 10.0)
    if not _tubegrip_move_relative(delta, rpm=rpm):
        return False, "move command failed", {}
    # The fixed 3s timeout this used to have was only ever validated against
    # small (~50 pulse) test moves. At the deliberately slow jog_rpm default,
    # a near-full-range move (thousands of pulses) genuinely takes longer
    # than that to arrive — confirmed on hardware 2026-08-12, where a 5%
    # move (4507 pulses) got cut off by the safety net at ~2053 pulses,
    # mid-motion, not stuck. Scale the wait with distance instead of
    # assuming every move is jog-sized; 300 pulses/s is a deliberately
    # conservative (slower than the ~680 pulses/s observed) estimate so
    # this errs toward waiting longer, not cutting off early again.
    timeout_s = max(3.0, abs(delta) / 300.0 + 2.0)
    final, settled = _tubegrip_observe_until_settled(sid, timeout_s=timeout_s)
    if not settled:
        # Safety net, same pattern as spinning_gripper.py's jog_experimental:
        # cut power if it never settled on its own rather than trust an
        # in-flight read.
        _write(sid, GRIPPER_REG_ENABLE, 0x0001)
        _write(sid, GRIPPER_REG_ENABLE, 0x0000)
        final2 = _read_int32(sid, 0x0004)
        final = final2 if final2 is not None else final
    diag = _tubegrip_diag(sid)
    if abs(delta) > 50 and final is not None and abs(final - start) <= 50 and _tubegrip_is_stuck(sid):
        return False, f"TubeGrip did not move and is stuck mid-move ({diag})", {}
    return True, "", {"start": start, "target": target, "final": final, "settled": settled, "diag": diag}


def _tubegrip_moveto_percent_autohome(percent: float, label: str) -> tuple[bool, str, dict]:
    """_tubegrip_moveto_percent, but if TubeGrip doesn't respond — not homed
    yet, position can't be read, move command not accepted — it homes
    TubeGrip (_tubegrip_home_sequence, which also clears a latched alarm)
    and tries once more. Only for moves made while TubeGrip holds nothing,
    since homing would drop whatever it holds.
    """
    ok, reason, info = _tubegrip_moveto_percent(percent)
    if ok:
        return ok, reason, info
    _log(f"  [{label}] TubeGrip not responding ({reason}) — homing it and trying again")
    home_ok, home_reason, _ = _tubegrip_home_sequence()
    if not home_ok:
        return False, f"{reason}; automatic home failed: {home_reason}", {}
    return _tubegrip_moveto_percent(percent)


TUBEGRIP_SHORT_OPEN_PULSES = 200   # an open ending further than this from its target is jammed, not just imprecise


def _tubegrip_open_step(label: str, auto_home: bool = False) -> bool:
    """Open-to-saved-target step for grab/place/tube_loop, which used to
    call the static gripper's _gripper_move directly — same idea, but the
    result is actually checked (unlike the fire-and-forget static-gripper
    calls) since a failed TubeGrip move (e.g. not homed yet) means a tube
    silently isn't released/grabbed, not just a cosmetic UI failure.
    """
    if auto_home:
        ok, reason, info = _tubegrip_moveto_percent_autohome(GRIPPER_TUBEGRIP["open_percent"], label)
    else:
        ok, reason, info = _tubegrip_moveto_percent(GRIPPER_TUBEGRIP["open_percent"])
    if not ok:
        _log(f"  [{label}] tubegrip open failed — {reason}")
        return False
    # The open target comes from home's measured open peak, which the jaws
    # can't reach under a normal move: seen on hardware 2026-10-28, open
    # 0% aimed at 5188 and stopped at 3682 with the driver still pushing
    # (~2 A, position-error warning). Left like that it ends up stuck
    # "running" or latches a position-deviation alarm by the next TubeGrip
    # command. An open holds nothing, so release the push straight away.
    final, target = info.get("final"), info.get("target")
    if final is not None and target is not None and abs(final - target) > TUBEGRIP_SHORT_OPEN_PULSES:
        _log(f"  [{label}] tubegrip open stopped short ({final} of {target}) — releasing the push")
        _tubegrip_release_push(label)
    return True


def _tubegrip_grab_step(label: str) -> bool:
    """Grab step for do_grab/tube_loop — uses the real torque-controlled
    collision-home grip (_tubegrip_grab), NOT a percent-of-range move like
    _tubegrip_open_step. A percent move just drives to a fixed position
    (whatever was calibrated with the jaws empty during homing) — it has no
    way to sense or clamp onto an actual tube, so on real hardware it either
    doesn't reach the tube (no grip) or can't reach the commanded position
    at all if the tube is in the way, without ever stalling/clamping onto
    it. Only a real crash (drive until the motor stalls against the tube)
    reliably grips regardless of the tube's exact diameter — confirmed
    2026-08-12 after switching grab/place to TubeGrip's percent-based close
    broke actually grabbing tubes (open/rotate were unaffected, since
    releasing doesn't need to sense anything and rotation is a different
    motor entirely).

    _tubegrip_grab only fires the crash command and returns as soon as the
    write succeeds — it doesn't wait for the motor to actually stall, so a
    caller chaining another action right after it (e.g. /decap's "open
    rotation gripper" step) could fire while TubeGrip was still mid-crash.
    Wait for position to stop changing (the same "stalled" signal
    _tubegrip_measure_close already uses) before treating the grab as done.
    """
    if not _tubegrip_grab():
        _log(f"  [{label}] tubegrip grab failed")
        return False
    sid = GRIPPER_TUBEGRIP["id"]
    _final, settled = _tubegrip_observe_until_settled(sid, timeout_s=8.0)
    if not settled:
        _log(f"  [{label}] tubegrip grab command sent but never stalled/settled")
        return False
    # A real crash re-zeros position at the stall point, same as the
    # /gripper/tubegrip/close endpoint's old crash-based behavior — keep
    # zero_ref in sync so later percent moves (e.g. the next "open"/place
    # step) compute from the right reference.
    GRIPPER_TUBEGRIP["zero_ref"] = "closed"
    return True


# ── Production-mirrored helpers ───────────────────────────────────────────────

def _write_mirrored(sids: list[int], addr: int, value: int) -> bool:
    """Writes the same register/value to every id for an axis (e.g. both y
    motors) so they receive identical config/commands in lockstep. Always
    writes to every id — doesn't short-circuit on the first failure —
    because a partial mirror (one motor configured, one not) is worse than
    a fully-reported failure. Returns True only if every write acked.
    """
    oks = [_write(sid, addr, value) for sid in sids]
    return all(oks)


def _write_per_motor_velocity(sids: list[int], cfg: dict) -> bool:
    """Writes REG_PR0_VEL to each id individually: cfg["rpm"] by default,
    overridden per id by cfg.get("rpm_per_id", {}) — a {motor id: rpm}
    dict for a ganged axis where one motor needs to run slightly slower/
    faster than the rest (e.g. one consistently running ahead of another).
    This is static per-move configuration, written before every move in
    the same place _move_config always wrote a single mirrored velocity —
    it is NOT a runtime correction and never touches a motor while it's
    already in motion.
    """
    overrides = cfg.get("rpm_per_id", {})
    oks = [_write(sid, REG_PR0_VEL, overrides.get(sid, cfg["rpm"])) for sid in sids]
    return all(oks)


def _move_config(sids: list[int], cfg: dict) -> None:
    _write_mirrored(sids, REG_CTRL_WORD, 0x1111)
    _write_mirrored(sids, REG_CTRL_WORD, 0x1122)
    _write_mirrored(sids, REG_AUTO_HOME, 0x0000)
    _write_mirrored(sids, REG_PR0_MODE,  0x01)
    _write_mirrored(sids, REG_MOTOR_DIR, cfg["dr"])
    _write_per_motor_velocity(sids, cfg)


def _clear_alarm(sids: list[int], cfg: dict) -> bool:
    # _move_config starts with the alarm-clear control words (0x1111, 0x1122)
    # and auto-home off — they used to be written twice in a row here.
    _move_config(sids, cfg)
    for _ in range(3):
        alarms = [_read(sid, REG_CUR_ALARM) for sid in sids]
        if all(alm is None or alm[0] == 0 for alm in alarms):
            return True
        time.sleep(0.05)
    alarms = [_read(sid, REG_CUR_ALARM) for sid in sids]
    bad = [(sid, alm[0]) for sid, alm in zip(sids, alarms) if alm and alm[0] != 0]
    if bad:
        for sid, code in bad:
            _log(f"    alarm persists after clear on motor {sid} — {_alarm_text(code)} (0x{code:04X})")
        return False
    return True


def _read_encoder_mm_one(sid: int, cfg: dict, once: bool = False) -> float | None:
    # high and low word in one request (0x602C/0x602D are adjacent) — half the bus traffic
    regs = (_read_once if once else _read)(sid, REG_ENC_POS_H, 2)
    if regs is None or len(regs) != 2:
        return None
    raw = (regs[0] << 16) | regs[1]
    if raw >= 0x8000_0000:
        raw -= 0x1_0000_0000
    return ((raw + _pos_offset_pulses.get(sid, 0)) / cfg["ppr"]) * cfg["mpr"]


def _driver_pulses(sid: int, mm: float, cfg: dict) -> int:
    """Absolute PR0 target, in the driver's own pulses, for app position mm."""
    return int(mm / cfg["mpr"] * cfg["ppr"]) - _pos_offset_pulses.get(sid, 0)


def _read_encoder_mm(sids: list[int], cfg: dict, once: bool = False) -> tuple[float | None, bool]:
    """Reads every motor id for this axis and returns (position_mm,
    diverged). position_mm is the average across all ids — the single
    position callers/UI report for the axis — or None if any id failed to
    read (same all-or-nothing failure semantics a single-motor read
    already had). diverged is True when there's more than one id and the
    spread between their individual readings exceeds
    cfg.get("max_divergence_mm", 1.0). This function only measures — it's
    the caller's job (see _hard_stop) to act on a True diverged the moment
    it sees one, not keep polling.
    """
    vals = []
    for sid in sids:
        v = _read_encoder_mm_one(sid, cfg, once)
        if v is None:
            return None, False
        vals.append(v)
    diverged = len(vals) > 1 and (max(vals) - min(vals)) > cfg.get("max_divergence_mm", 3.0)
    return sum(vals) / len(vals), diverged


def _hard_stop(sids: list[int], cfg: dict) -> None:
    """Best-effort emergency stop for a diverged multi-motor axis: commands
    each motor to re-target wherever IT individually currently is (instead
    of the move's original shared destination) and re-triggers Path 0 —
    the same PR0-position + trigger registers a normal move already uses,
    just with target==current so each motor decelerates to a stop right
    where it is rather than continuing toward the original target.

    This driver exposes no documented hard-abort/E-stop register — neither
    REG_CTRL_WORD's own comment nor openshelf_hardware's register table
    names one — so "command zero further distance from here" is the
    closest verified-safe primitive available in this register set; it has
    NOT been hardware-validated against a genuine mid-move divergence
    (only exercised in isolation). Also sets _stop_flag so home/extend_to's
    polling loop — for this axis and any other running in parallel —
    stops issuing anything further.
    """
    _stop_flag.set()
    for sid in sids:
        pos = _read_encoder_mm_one(sid, cfg)
        if pos is None:
            continue
        pulses = _driver_pulses(sid, pos, cfg)
        _write(sid, REG_PR0_POS_H, (pulses >> 16) & 0xFFFF)
        _write(sid, REG_PR0_POS_L, pulses & 0xFFFF)
        _write(sid, TRIGGER, TRIG_PATH0)


# How far an individual motor is allowed to sit from the exact commanded
# target once a multi-motor move has settled, before _sync_correct nudges
# it back — must stay well under any axis's max_divergence_mm so this never
# fights the hard-stop, and above MOVE_TOLERANCE_MM so it isn't chasing
# read noise.
SYNC_CORRECT_TOLERANCE_MM = 0.3


def _sync_correct(sids: list[int], cfg: dict, target_mm: float, label: str) -> None:
    """Repeated recurring divergence-threshold trips on a multi-motor axis
    are rarely one dramatic event — far more often it's a small tracking
    error on one motor, too small to trip the hard-stop on any single move,
    that isn't corrected before the next move starts. Left alone, that
    residual error carries into the next move's starting position and
    compounds move after move until it eventually does cross the
    threshold. This closes that gap: once a move has already settled (both
    motors stationary — this is never called while a move is in flight, so
    it can't fight or interfere with one), individually check each motor
    against the exact commanded target and issue a small corrective move
    for any that's off by more than SYNC_CORRECT_TOLERANCE_MM, pinning
    both motors to the same precise position after every single move
    instead of letting per-move error accumulate silently.
    """
    if len(sids) < 2:
        return
    for sid in sids:
        pos = _read_encoder_mm_one(sid, cfg)
        if pos is None or abs(pos - target_mm) <= SYNC_CORRECT_TOLERANCE_MM:
            continue
        _log(f"  [{label}] sync correction — motor {sid} at {pos:.2f}mm, nudging to {target_mm:.2f}mm")
        pulses = _driver_pulses(sid, target_mm, cfg)
        _write(sid, REG_PR0_ACCEL, cfg["accel"])
        _write(sid, REG_PR0_DECEL, cfg["accel"])
        _write(sid, REG_PR0_POS_H, (pulses >> 16) & 0xFFFF)
        _write(sid, REG_PR0_POS_L, pulses & 0xFFFF)
        _write(sid, TRIGGER, TRIG_PATH0)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3.0:
            p = _read_encoder_mm_one(sid, cfg)
            if p is not None and abs(p - target_mm) <= SYNC_CORRECT_TOLERANCE_MM:
                break
            time.sleep(0.05)


def _set_motor_holding_current(sid: int, pct: int) -> bool:
    """Write REG_HOLDING_CURRENT_PCT (Pr5.01) and read-verify it took. This
    driver has no software force-disable (see REG_MOTOR_ENABLE comment), so
    manual mode uses this instead — 0% holding current to let the axis be
    moved by hand, restored to its prior value to leave it holding position
    again afterward.
    """
    if not _write(sid, REG_HOLDING_CURRENT_PCT, pct):
        _log(f"    [motor {sid}] holding-current write failed (no ack)")
        return False
    readback = _read(sid, REG_HOLDING_CURRENT_PCT)
    if readback is None:
        _log(f"    [motor {sid}] holding-current write acked but readback failed — can't confirm it took")
        return False
    if readback[0] != pct:
        _log(f"    [motor {sid}] holding-current write acked but register reads back {readback[0]}% (wanted {pct}%) — did not take")
        return False
    return True


# ── Home ──────────────────────────────────────────────────────────────────────

def _home_motor(name: str, cfg: dict) -> bool:
    if not cfg.get("enabled", True):
        _log(f"  [{name}] axis disabled — skipping (no motor installed)")
        return False

    sids     = cfg["ids"]
    home_dir = cfg.get("home_dir", 1)

    _log(f"  [{name}] clearing alarm...")
    _clear_alarm(sids, cfg)

    _write_mirrored(sids, REG_CTRL_WORD, 0x1111)
    _write_mirrored(sids, REG_CTRL_WORD, 0x1122)
    _write_mirrored(sids, REG_AUTO_HOME, 0x0000)
    _write_mirrored(sids, REG_PR0_MODE,  0x02)
    _write_mirrored(sids, REG_MOTOR_DIR, home_dir)
    _write_mirrored(sids, REG_PR0_VEL,   cfg["home_rpm"])
    # Homing never used to set its own acceleration here at all — it just
    # inherited whatever REG_PR0_ACCEL/DECEL a previous extend_to() call
    # (or the drive's own power-on state) happened to leave behind, which
    # is unpredictable and could be far more aggressive than intended for
    # a search that ends by crashing into a hard stop. home_accel is a
    # deliberately gentle, explicit ramp for the homing search specifically
    # (this register is "acceleration TIME per 1000rpm" — a larger number
    # is a slower/gentler ramp, not a faster one).
    home_accel = cfg.get("home_accel", cfg["accel"])
    _write_mirrored(sids, REG_PR0_ACCEL, home_accel)
    _write_mirrored(sids, REG_PR0_DECEL, home_accel)

    # For a ganged axis, grab each motor's position right before triggering
    # as the first "previous tick" reading — the loop below confirms every
    # motor actually started by watching for tick-to-tick movement (this
    # tick vs the last one it read, not this tick vs a fixed baseline), not
    # by racing a clock. Matching *speed* isn't separately verified at
    # runtime — REG_PR0_VEL above was just mirrored identically to every
    # id, which is what guarantees it.
    last_positions = {sid: _read_encoder_mm_one(sid, cfg) for sid in sids} if len(sids) > 1 else None
    started         = {sid: False for sid in sids} if len(sids) > 1 else None

    _write_mirrored(sids, TRIGGER, TRIG_PATH0)

    _log(f"  [{name}] homing...")
    start           = time.monotonic()
    has_moved       = False
    start_confirmed = last_positions is None  # nothing to confirm on a single-motor axis

    while time.monotonic() - start < HOME_TIMEOUT:
        if _stop_flag.is_set():
            _log(f"  [{name}] stopped")
            return False

        # Confirm every motor on a ganged axis has shown real tick-to-tick
        # movement at some point — catches one motor silently failing to
        # receive/execute the move, without gating on elapsed time; a motor
        # that never moves at all still falls through to HOME_TIMEOUT below,
        # same as any other stuck-drive case. Deliberately NOT a continuing
        # position-divergence check: that would false-trigger the instant
        # one motor legitimately reaches its hard stop before the other.
        # Stops being checked for the rest of this call once confirmed.
        if not start_confirmed:
            now_positions = {sid: _read_encoder_mm_one(sid, cfg) for sid in sids}
            for sid in sids:
                if (not started[sid] and now_positions[sid] is not None and last_positions[sid] is not None
                        and abs(now_positions[sid] - last_positions[sid]) >= MOTOR_MOVED_EPSILON_MM):
                    started[sid] = True
            last_positions = now_positions
            if all(started.values()):
                start_confirmed = True

        statuses = [_read(sid, REG_MOTION_ST) for sid in sids]
        if any(s is None for s in statuses):
            time.sleep(0.05)
            continue
        is_complete = all(bool(s[0] & (1 << STATUS_COMPLETE_BIT)) for s in statuses)
        alarms      = [_read(sid, REG_CUR_ALARM) for sid in sids]
        alarm_codes = [a[0] if a else None for a in alarms]
        if not has_moved and time.monotonic() - start > 0.5:
            has_moved = True
        if any(c == ALARM_LIMIT_STOP for c in alarm_codes) and has_moved:
            _log(f"  [{name}] limit stop ({time.monotonic()-start:.1f}s)")
            break
        if is_complete and (has_moved or time.monotonic() - start > 0.5):
            _log(f"  [{name}] complete ({time.monotonic()-start:.1f}s)")
            break
        bad = [(sid, c) for sid, c in zip(sids, alarm_codes) if c not in (None, 0, ALARM_LIMIT_STOP)]
        if bad:
            msg = "FAULT during homing — " + ", ".join(
                f"motor {sid}: {_alarm_text(c)} (0x{c:04X})" for sid, c in bad)
            _log(f"  [{name}] {msg}")
            _track_error(name, msg)
            return False
        time.sleep(0.05)
    else:
        _log(f"  [{name}] TIMEOUT during homing")
        _track_error(name, "TIMEOUT during homing")
        return False

    # For a ganged axis, each motor's zero is set from wherever IT
    # individually is right now — TRIG_SET_ZERO below makes every motor
    # read exactly 0 immediately after, regardless of whether they're
    # actually at the same physical point, which would silently erase any
    # real mechanical skew from the encoder-divergence check going
    # forward (it only ever sees the difference *from here on*). Check
    # while there's still something meaningful to check against: log a
    # warning if the two didn't land in the same physical place, since
    # that's a mechanical issue (limit switches not physically aligned,
    # one side's hard stop reached early) no software correction here
    # should paper over by silently nudging a motor that already hit its
    # physical hard stop.
    if len(sids) > 1:
        raw_positions = {sid: _read_encoder_mm_one(sid, cfg) for sid in sids}
        vals = [v for v in raw_positions.values() if v is not None]
        if len(vals) > 1 and (max(vals) - min(vals)) > SYNC_CORRECT_TOLERANCE_MM:
            _log(f"  [{name}] WARNING — motors landed {max(vals) - min(vals):.2f}mm apart before "
                 f"zeroing ({raw_positions}) — check physical alignment (limit switches, hard stops)")

    _write_mirrored(sids, TRIGGER, TRIG_SET_ZERO)
    for sid in sids:
        _pos_offset_pulses.pop(sid, None)
    time.sleep(0.1)
    _move_config(sids, cfg)
    _is_homed[name] = True
    _log(f"  [{name}] homed — position = 0.0")
    return True


def home(name: str) -> bool:
    return _home_motor(name, MOTORS[name])


# Both vertical actuators (z_gripper, z_pipettor) ride the same moving
# gantry as x/y, so BOTH have to be retracted before any x/y move — an
# un-homed one left hanging down would crash into things as the gantry
# travels horizontally. Sequential, not parallel: without confirmed
# physical clearance between the two heads, homing them one at a time is
# the safer default even though it costs a bit more time. Skips whichever
# is currently disabled (vacuously succeeds if neither is enabled).
_Z_AXES_TO_RETRACT_BEFORE_XY = ["z_gripper", "z_pipettor"]


def _home_all_z(label: str) -> bool:
    for name in _Z_AXES_TO_RETRACT_BEFORE_XY:
        if not MOTORS[name].get("enabled", True):
            continue
        _log(f"  [{label}] homing {name}")
        if not home(name):
            _log(f"  [{label}] {name} home failed — aborting")
            return False
        if _stop_flag.is_set():
            return False
    return True


def _home_all_z_for_plate_move(label: str) -> bool:
    """Same as _home_all_z, plus closes the spinning (decapper) gripper to
    its default resting rotation right after — used before any x/y move
    toward a tube plate well, so the rotating collar is never left in an
    open/protruding orientation while the head travels across the plate
    grid. Only for moves onto the tube PLATE — moves to the tube holder or
    cap holder manage the spin gripper's state themselves.
    """
    if not _home_all_z(label):
        return False
    if _stop_flag.is_set():
        return False
    _log(f"  [{label}] closing spinning gripper")
    if not _spin_move(GRIPPER_SPIN["close_pos"]):
        _log(f"  [{label}] spin close failed — aborting")
        return False
    return True


# ── Encoder positions ─────────────────────────────────────────────────────────

def read_positions(once: bool = False) -> dict:
    result = {}
    for name, cfg in MOTORS.items():
        if not cfg.get("enabled", True):
            result[name] = {"disabled": True, "val": None, "pulses": None, "unit": "mm", "error": False, "diverged": False}
            continue
        val, diverged = _read_encoder_mm(cfg["ids"], cfg, once)
        if val is None:
            result[name] = {"error": True, "val": None, "pulses": None, "unit": "mm", "diverged": False}
            continue
        pulses = int(round(val / cfg["mpr"] * cfg["ppr"]))
        result[name] = {"val": round(val, 2), "unit": "mm", "pulses": pulses, "error": False, "diverged": diverged}
        if diverged:
            _log(f"  [{name}] WARNING — motor positions diverged by more than "
                 f"{cfg.get('max_divergence_mm', 1.0)}mm")
    return result


# ── Extend ────────────────────────────────────────────────────────────────────

def extend_to(name: str, target_mm: float | None = None) -> bool:
    cfg = MOTORS[name]
    if not cfg.get("enabled", True):
        _log(f"  [{name}] axis disabled — skipping (no motor installed)")
        return False

    sids = cfg["ids"]
    lim = target_mm if target_mm is not None else cfg["lim"]

    if lim > cfg["lim"]:
        _log(f"  [{name}] {lim:.1f} mm exceeds limit {cfg['lim']:.1f} mm")
        return False
    if lim < 0:
        _log(f"  [{name}] target must be >= 0 mm")
        return False
    if not _is_homed[name]:
        _log(f"  [{name}] not homed — home first")
        return False

    _clear_alarm(sids, cfg)
    current_mm, diverged = _read_encoder_mm(sids, cfg)
    if diverged:
        _hard_stop(sids, cfg)
        msg = f"FAULT — motors already diverged by more than {cfg.get('max_divergence_mm', 1.0)}mm before move"
        _log(f"  [{name}] {msg}")
        _track_error(name, msg)
        return False
    if current_mm is not None:
        direction = "forward" if lim > current_mm else "reverse"
        _log(f"  [{name}] {current_mm:.2f} mm → {lim:.1f} mm ({direction})")
    else:
        _log(f"  [{name}] → {lim:.1f} mm")

    _write_mirrored(sids, REG_PR0_ACCEL, cfg["accel"])
    _write_mirrored(sids, REG_PR0_DECEL, cfg["accel"])
    if any(_pos_offset_pulses.get(sid, 0) for sid in sids):
        # re-zeroed after manual mode: each driver needs its own target
        for sid in sids:
            abs_pulses = _driver_pulses(sid, lim, cfg)
            _write(sid, REG_PR0_POS_H, (abs_pulses >> 16) & 0xFFFF)
            _write(sid, REG_PR0_POS_L, abs_pulses & 0xFFFF)
    else:
        abs_pulses = int(lim / cfg["mpr"] * cfg["ppr"])
        _write_mirrored(sids, REG_PR0_POS_H, (abs_pulses >> 16) & 0xFFFF)
        _write_mirrored(sids, REG_PR0_POS_L, abs_pulses & 0xFFFF)
    _write_mirrored(sids, TRIGGER, TRIG_PATH0)
    time.sleep(0.1)

    # move_start_mm anchors has_moved to a real measured displacement
    # instead of elapsed time — see the bug this fixes, below.
    move_start_mm = current_mm
    start     = time.monotonic()
    has_moved = False

    while time.monotonic() - start < MOVE_TIMEOUT:
        if _stop_flag.is_set():
            _log(f"  [{name}] stopped")
            return False

        enc_mm, diverged = _read_encoder_mm(sids, cfg)
        if diverged:
            _hard_stop(sids, cfg)
            msg = f"FAULT — motors diverged by more than {cfg.get('max_divergence_mm', 1.0)}mm mid-move"
            _log(f"  [{name}] {msg}")
            _track_error(name, msg)
            return False
        # Confirmed bug (2026-09-09, z_gripper): has_moved used to flip
        # True purely from elapsed time (> 0.5s), regardless of whether the
        # motor had actually moved. A motor that's blocked from the very
        # start (e.g. immediately re-trips a limit-stop alarm) would sit at
        # its starting position while that 0.5s elapsed, then the alarm
        # branch below would see has_moved==True and treat the persisting
        # alarm as "hit a real limit mid-move" (success) instead of "never
        # moved at all" (failure) — reporting the move complete while the
        # encoder never left its starting value. Anchoring has_moved to a
        # real measured displacement instead closes that gap.
        if not has_moved and enc_mm is not None and move_start_mm is not None \
                and abs(enc_mm - move_start_mm) >= MOTOR_MOVED_EPSILON_MM:
            has_moved = True
        if enc_mm is not None:
            if abs(enc_mm - lim) <= MOVE_TOLERANCE_MM and (has_moved or time.monotonic() - start > 0.5):
                _log(f"  [{name}] confirmed — {enc_mm:.2f} mm (target {lim:.1f} mm)")
                _sync_correct(sids, cfg, lim, name)
                return True
        statuses = [_read(sid, REG_MOTION_ST) for sid in sids]
        if all(s is not None for s in statuses):
            complete  = all(bool(s[0] & (1 << STATUS_COMPLETE_BIT)) for s in statuses)
            has_fault = any(bool(s[0] & (1 << STATUS_FAULT_BIT)) for s in statuses) if cfg.get("fault_bit") else False
            if has_fault:
                _hard_stop(sids, cfg)
                _log(f"  [{name}] FAULT — drive fault bit set (check drive display)")
                _track_error(name, "drive fault bit set")
                return False
            # No elapsed-time fallback here (unlike "confirmed" above, which
            # is already gated on actually being within MOVE_TOLERANCE_MM
            # of the target regardless of has_moved) — trusting the drive's
            # own "complete" bit after some time had simply passed is the
            # same bug as the limit-stop branch below: this drive can
            # report complete=True having never actually moved. Requiring
            # has_moved (real measured displacement) closes that gap here
            # too.
            if complete and has_moved:
                enc_mm, _ = _read_encoder_mm(sids, cfg)
                pos_str = f"{enc_mm:.2f} mm" if enc_mm is not None else "unknown"
                _log(f"  [{name}] complete — {pos_str} (target {lim:.1f} mm)")
                _sync_correct(sids, cfg, lim, name)
                return True
        alarms      = [_read(sid, REG_CUR_ALARM) for sid in sids]
        alarm_codes = [a[0] if a else None for a in alarms]
        if any(c == ALARM_LIMIT_STOP for c in alarm_codes) and has_moved:
            enc_mm, _ = _read_encoder_mm(sids, cfg)
            pos_str = f"{enc_mm:.2f} mm" if enc_mm is not None else "unknown"
            _log(f"  [{name}] limit stop — {pos_str}")
            return True
        if any(c == ALARM_LIMIT_STOP for c in alarm_codes) and not has_moved:
            msg = "limit stop at start of move (re-home first)"
            _log(f"  [{name}] FAULT — {msg}")
            _track_error(name, f"FAULT — {msg}")
            return False
        bad = [(sid, c) for sid, c in zip(sids, alarm_codes) if c not in (None, 0)]
        if bad:
            msg = "FAULT — " + ", ".join(f"motor {sid}: {_alarm_text(c)} (0x{c:04X})" for sid, c in bad)
            _log(f"  [{name}] {msg}")
            _track_error(name, msg)
            return False
        time.sleep(0.05)

    enc_mm, _ = _read_encoder_mm(sids, cfg)
    pos_str = f"{enc_mm:.2f} mm" if enc_mm is not None else "unknown"
    _log(f"  [{name}] TIMEOUT — {pos_str} (target {lim:.1f} mm)")
    _track_error(name, f"TIMEOUT at {pos_str} (target {lim:.1f} mm)")
    return False


# ── Loop ──────────────────────────────────────────────────────────────────────

def run_loop(cycles: int, motors: list[str]) -> None:
    _log(f"  Starting {cycles} cycle(s) — motors: {', '.join(motors)} — press Stop to abort")

    _log("  [home]")
    for name in motors:
        if _stop_flag.is_set():
            return
        if not home(name):
            _log(f"  Loop aborted — initial homing {name} failed")
            return

    for i in range(cycles):
        if _stop_flag.is_set():
            _log("  Loop stopped")
            return
        targets = {name: round(random.uniform(0, MOTORS[name]["lim"]), 1) for name in motors}
        target_str = "  ".join(f"{n}={t:.1f}" for n, t in targets.items())
        _log(f"── Cycle {i+1}/{cycles}  targets: {target_str}")
        for name, target in targets.items():
            if _stop_flag.is_set():
                _log("  Loop stopped")
                return
            if not extend_to(name, target):
                _log(f"  Loop aborted — move {name} to {target:.1f} mm failed")
                return

    _log("  [home]")
    for name in motors:
        if _stop_flag.is_set():
            return
        if not home(name):
            _log(f"  Loop aborted — final homing {name} failed")
            return

    _log(f"  Loop complete — {cycles} cycle(s) done")


def test_all() -> None:
    for name in list(MOTORS):
        if _stop_flag.is_set():
            return
        cfg = MOTORS[name]
        if not cfg.get("enabled", True):
            _log(f"── [{name}] disabled — skipping")
            continue
        _log(f"── [{name}] home → max → home")
        if not home(name):
            _log(f"  test_all aborted — homing {name} failed")
            return
        if not extend_to(name, cfg["lim"]):
            _log(f"  test_all aborted — extending {name} failed")
            return
        if not home(name):
            _log(f"  test_all aborted — final homing {name} failed")
            return
    _log("  test_all complete")


# ── Background task runner ────────────────────────────────────────────────────

def _is_busy() -> bool:
    return _op_thread is not None and _op_thread.is_alive()


def _bg_refusal() -> str:
    return "manual mode is on — exit manual mode first" if _manual_mode else "busy"


def _motion_refused() -> Optional[str]:
    """Checks for endpoints that move a motor directly (not via _run_bg):
    one motor operation at a time, nothing while in manual mode."""
    if not _connected:
        return "not connected"
    if _is_busy():
        return "busy"
    if _manual_mode:
        return "manual mode is on — exit manual mode first"
    return None


def _run_bg(fn, *args) -> bool:
    global _op_thread
    if _is_busy() or _manual_mode:
        return False
    _stop_flag.clear()
    _op_thread = threading.Thread(target=fn, args=args, daemon=True)
    _op_thread.start()
    return True


# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(title="Paul Cherry Picker")
# Branding (fonts, logo), same files as zeon_cherrypicker. check_dir=False: a
# missing folder only loses the styling, it must never stop the server starting.
app.mount("/assets", StaticFiles(directory=Path(__file__).parent / "assets", check_dir=False), name="assets")


def _ok(**kw):
    return JSONResponse({"ok": True, **kw})


def _err(msg: str, code: int = 400):
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


class LoginReq(BaseModel):
    user:    str
    machine: str


class ConnectReq(BaseModel):
    port: str = "auto"   # "auto" (or blank) = find the motor bus by probing
    baudrate: int = 115200


class HomeReq(BaseModel):
    motor: str
    auto: bool = False   # True for the automatic home all right after connect


class ExtendReq(BaseModel):
    motor: str
    mm: Optional[float] = None


class ExtendXYZReq(BaseModel):
    x: Optional[float] = None
    y: Optional[float] = None


class LoopReq(BaseModel):
    cycles: int
    motors: list[str] = ["y", "x"]


class LimitsReq(BaseModel):
    y: float
    x: float
    z_gripper: Optional[float] = None   # optional/partial-update — None means "leave unchanged"
    z_pipettor: Optional[float] = None


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(HTML)


@app.post("/login")
def login(req: LoginReq):
    global _current_user, _current_machine, _session_start
    user    = req.user.strip()
    machine = req.machine.strip()
    if not user:
        return _err("username cannot be empty")
    if not machine:
        return _err("machine ID cannot be empty")
    if _current_user:
        return _err(f"already logged in as {_current_user}")
    _current_user    = user
    _current_machine = machine
    _session_start   = datetime.datetime.now()
    _log(f"[session] {user} ({machine}) logged in at {_session_start.strftime('%Y-%m-%d %H:%M:%S')}")
    return _ok(user=user, machine=machine)


@app.post("/logout")
def logout():
    global _current_user, _current_machine, _session_start, _connected, _client, _current_ops, _current_errors
    if not _current_user:
        return _ok()
    end = datetime.datetime.now()
    _log(f"[session] {_current_user} ({_current_machine}) logged out at {end.strftime('%Y-%m-%d %H:%M:%S')}")
    if _session_start:
        _save_session(_current_user, _current_machine or "", _session_start, end,
                      list(_current_ops), list(_current_errors))
    if _client:
        _client.close()
    _connected       = False
    _current_user    = None
    _current_machine = None
    _session_start   = None
    _current_ops     = []
    _current_errors  = []
    return _ok()


@app.get("/sessions")
def get_sessions():
    return {"sessions": _load_sessions()}


class NoteReq(BaseModel):
    text: str


def _load_notes() -> list:
    if NOTES_FILE.exists():
        try:
            return json.loads(NOTES_FILE.read_text())
        except Exception:
            pass
    return []


@app.get("/notes")
def get_notes():
    return {"notes": _load_notes()}


@app.post("/notes")
def post_note(req: NoteReq):
    if not _current_user:
        return _err("not logged in")
    text = req.text.strip()
    if not text:
        return _err("note cannot be empty")
    notes = _load_notes()
    notes.append({
        "user":    _current_user,
        "machine": _current_machine,
        "time":    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "text":    text,
    })
    NOTES_FILE.write_text(json.dumps(notes, indent=2))
    return _ok()


@app.get("/errors")
def get_errors():
    flat = []
    for s in _load_sessions():
        for e in s.get("errors", []):
            flat.append({
                "date":        s.get("login", "")[:10],
                "user":        s.get("user", ""),
                "machine":     s.get("machine", ""),
                "time":        e.get("time", ""),
                "motor":       e.get("motor", ""),
                "description": e.get("description", ""),
            })
    return {"errors": flat}


@app.get("/readme")
def get_readme():
    for name in ("PAUL_README.md", "README.md"):
        path = Path(__file__).parent / name
        if path.exists():
            return {"content": path.read_text()}
    return {"content": "README not found."}


@app.post("/connect")
def connect(req: ConnectReq):
    if _is_busy():
        return _err("busy")
    global _bus_auto, _bus_lost
    port = req.port.strip()
    _bus_auto = _is_auto(port)
    _bus_lost = False
    if _bus_auto:
        # Free both ports first so a reconnect can re-probe them.
        if _client:
            _client.close()
        _gripper_disconnect()
        port = _find_motor_bus(req.baudrate, skip=_ports_to_skip(quiet=False))
        if port is None:
            return _err("motor bus not found on any USB serial port — is it plugged in and powered?")
    ok = _do_connect(port, req.baudrate)
    if ok:
        _log_op(f"connect {port} @ {req.baudrate}")
        # Spin holds torque by default — only the release button turns it
        # off. The driver can power up released, so enable it on every connect.
        held = _spin_set_enabled(True)
        _log(f"  [spin] {'holding torque enabled' if held else 'enable on connect FAILED — Spin not responding'}")
        # Find and open the static gripper now rather than on its first use,
        # so a missing one shows up at connect time.
        with _gripper_lock:
            _gripper_connect()
    if _static_gripper_disabled():
        _log("  [gripper] static gripper search disabled — not looking for it")
        gripper_port = "disabled"
    else:
        gripper_port = _gripper_port if _is_auto(GRIPPER["port"]) else GRIPPER["port"]
    return (_ok(connected=ok, port=port, gripper_port=gripper_port) if ok
            else _err(f"failed to connect to {port}"))


@app.post("/disconnect")
def disconnect():
    global _connected, _client, _manual_mode
    if _is_busy():
        return _err("busy — stop operation first")
    global _bus_auto, _bus_lost
    if _client:
        _client.close()
    _gripper_disconnect()
    global _laser_live
    _connected = False
    _bus_auto = False
    _bus_lost = False
    _manual_mode = False
    _laser_live = False
    _forget_homing("disconnected")
    _log("[bus] disconnected")
    _log_op("disconnect")
    return _ok()


@app.get("/state")
def get_state():
    pos = read_positions() if _connected else {}
    if _connected:
        GRIPPER_SPIN["position"] = _read_int32(GRIPPER_SPIN["id"], 0x0004)
        GRIPPER_TUBEGRIP["position"] = _read_int32(GRIPPER_TUBEGRIP["id"], 0x0004)
    return {
        "user":         _current_user,
        "machine":      _current_machine,
        "connected":    _connected,
        "busy":         _is_busy(),
        "homed":        dict(_is_homed),
        "manual_mode":  _manual_mode,
        "showcase_phase": _showcase_phase,
        "showcase_loop": _showcase_loop_progress,
        "showcase_loop_last": _showcase_loop_last,
        "positions":    pos,
        "limits":       {name: cfg["lim"] for name, cfg in MOTORS.items()},
        "tubegrip":     GRIPPER_TUBEGRIP,
        "spin":         GRIPPER_SPIN,
        "ports":        {"bus": _bus_port if _connected else None,
                         "gripper": _gripper_port if _is_auto(GRIPPER["port"]) else GRIPPER["port"],
                         "pipettor": _pipettor_info.get("port") if _pipettor_info.get("connected") else None},
    }


@app.get("/positions")
def get_positions():
    if not _connected:
        return _err("not connected")
    return {"ok": True, "positions": read_positions()}


@app.get("/diag/per_motor/{name}")
def get_per_motor_positions(name: str):
    """Read-only diagnostic: each individual motor id's raw encoder
    position (mm) for a ganged axis, plus the spread between them. Never
    writes anything to a drive.
    """
    if not _connected:
        return _err("not connected")
    if name not in MOTORS:
        return _err(f"unknown motor '{name}'")
    cfg = MOTORS[name]
    positions = {}
    for sid in cfg["ids"]:
        v = _read_encoder_mm_one(sid, cfg)
        positions[sid] = round(v, 3) if v is not None else None
    vals = [v for v in positions.values() if v is not None]
    spread = round(max(vals) - min(vals), 3) if len(vals) > 1 else 0.0
    return {"ok": True, "motor": name, "positions": positions, "spread_mm": spread,
            "max_divergence_mm": cfg.get("max_divergence_mm")}


def _home_all_sequence() -> bool:
    """Home all: opens the static gripper, homes TubeGrip, retracts the z
    axes, then homes y and x. Returns True when z_gripper, y and x all ended
    up homed (a TubeGrip home failure is logged but doesn't stop it), and
    starts the live laser readings in that case."""
    global _laser_live
    _laser_live = False
    _log("  [gripper] opening before home all")
    _gripper_move(GRIPPER_CMD_OPEN)
    _log("  [tubegrip] homing as part of home all")
    ok, reason, _info = _tubegrip_home_sequence()
    if not ok:
        _log(f"  [tubegrip] home FAILED during home all — {reason}")
    if _stop_flag.is_set():
        return False
    # Retract both z axes before homing y/x — homing y or x moves
    # them, which is exactly the x/y movement _home_all_z exists to
    # protect an un-retracted z axis against (see the comment above
    # its definition, and every other x/y-moving function).
    if not _home_all_z("home all"):
        return False
    for n in ("y", "x"):
        if _stop_flag.is_set():
            break
        home(n)
    # rotation has no homing search — its home is the calibrated close position (as in zeon_cherrypicker.py)
    if not _stop_flag.is_set():
        _log("  [spin] moving to close position as part of home all")
        if not _spin_move(GRIPPER_SPIN["close_pos"]):
            _log("  [spin] close FAILED during home all")
    axes = [n for n in ("z_gripper", "y", "x") if MOTORS[n].get("enabled", True)]
    if _stop_flag.is_set() or not all(_is_homed[n] for n in axes):
        return False
    _laser_live = True
    _log("  [laser] home all complete — live laser readings started")
    return True


@app.post("/home")
def do_home(req: HomeReq):
    if not _connected:
        return _err("not connected")
    if _is_busy():
        return _err("busy")
    if req.auto and _showcase_phase == "await_finish":
        # homing opens TubeGrip — it would drop the cap off the open tube
        _log("[auto home] skipped — TubeGrip is holding a cap; press finished (recap), then home all")
        return _err("auto home skipped — TubeGrip is holding a cap; press finished, then home all")
    if req.motor == "all":
        names = _ALL_MOTORS
    elif req.motor in _ALL_MOTORS:
        names = [req.motor]
    else:
        return _err(f"unknown motor: {req.motor}")

    def _task():
        if req.motor == "all":
            _home_all_sequence()
        else:
            for n in names:
                if _stop_flag.is_set():
                    break
                home(n)

    _log_op(f"home {req.motor}")
    if not _run_bg(_task):
        return _err(_bg_refusal())
    return _ok(started=True)


@app.post("/extend")
def do_extend(req: ExtendReq):
    if not _connected:
        return _err("not connected")
    if _is_busy():
        return _err("busy")
    if req.motor == "all":
        names = list(MOTORS)
    elif req.motor in MOTORS:
        names = [req.motor]
    else:
        return _err(f"unknown motor: {req.motor}")

    def _task():
        for n in names:
            if _stop_flag.is_set():
                break
            extend_to(n, req.mm)

    _log_op(f"extend {req.motor} to {req.mm if req.mm is not None else 'max'}mm")
    if not _run_bg(_task):
        return _err(_bg_refusal())
    return _ok(started=True)


@app.post("/extend/xyz")
def do_extend_xyz(req: ExtendXYZReq):
    if not _connected:
        return _err("not connected")
    if _is_busy():
        return _err("busy")

    def _task():
        # Always home both z_gripper and z_pipettor before any x/y
        # movement — see _home_all_z. Skips whichever is disabled; if
        # neither is enabled there's simply nothing to home before x+y move.
        # z-axis target moves themselves now live in their own dedicated
        # panel/endpoints (/home, /extend with motor=z_gripper/z_pipettor),
        # not here — this endpoint only ever moves x+y.
        if not _home_all_z("extend xyz"):
            return
        if _stop_flag.is_set():
            return

        # Move x and y in parallel, wait for both
        threads = []
        for name, target in [("y", req.y), ("x", req.x)]:
            if target is None or _stop_flag.is_set():
                continue
            def _move(n=name, t=target):
                extend_to(n, t)
            th = threading.Thread(target=_move, daemon=True)
            threads.append(th)
            th.start()
        for th in threads:
            th.join()

    parts = []
    if req.x is not None: parts.append(f"x={req.x}")
    if req.y is not None: parts.append(f"y={req.y}")
    _log_op(f"extend xyz {' '.join(parts)}")
    if not _run_bg(_task):
        return _err(_bg_refusal())
    return _ok(started=True)


@app.post("/loop")
def do_loop(req: LoopReq):
    if not _connected:
        return _err("not connected")
    if _is_busy():
        return _err("busy")
    if req.cycles < 1:
        return _err("cycles must be >= 1")
    valid = [m for m in req.motors if m in _ALL_MOTORS]
    if not valid:
        return _err("no valid motors selected")
    _log_op(f"loop {req.cycles} cycles [{', '.join(valid)}]")
    if not _run_bg(run_loop, req.cycles, valid):
        return _err(_bg_refusal())
    return _ok(started=True)


@app.post("/limits")
def set_limits(req: LimitsReq):
    if req.y <= 0 or req.x <= 0:
        return _err("all limits must be > 0")
    if req.z_gripper is not None and req.z_gripper <= 0:
        return _err("all limits must be > 0")
    if req.z_pipettor is not None and req.z_pipettor <= 0:
        return _err("all limits must be > 0")
    MOTORS["y"]["lim"] = req.y
    MOTORS["x"]["lim"] = req.x
    parts = [f"y={req.y}mm", f"x={req.x}mm"]
    if req.z_gripper is not None:
        MOTORS["z_gripper"]["lim"] = req.z_gripper
        parts.append(f"z_gripper={req.z_gripper}mm")
    if req.z_pipettor is not None:
        MOTORS["z_pipettor"]["lim"] = req.z_pipettor
        parts.append(f"z_pipettor={req.z_pipettor}mm")
    _save_limits()
    _log(f"  limits updated — {'  '.join(parts)} (saved to {LIMITS_FILE.name})")
    _log_op(f"set limits {' '.join(parts)}")
    return _ok(y=req.y, x=req.x,
               z_gripper=MOTORS["z_gripper"]["lim"], z_pipettor=MOTORS["z_pipettor"]["lim"])


@app.post("/test_all")
def do_test_all():
    if not _connected:
        return _err("not connected")
    if _is_busy():
        return _err("busy")
    _log_op("test all")
    if not _run_bg(test_all):
        return _err(_bg_refusal())
    return _ok(started=True)


@app.post("/stop")
def do_stop():
    _stop_flag.set()
    _log("  [stop requested]")
    _log_op("stop")
    return _ok()


REG_PROFILE_POS_H = 0x602A   # Pr8.42/8.43 commanded (profile) position, high word first
REZERO_MATCH_PULSES = 200    # commanded vs actual after the re-zero — beyond this, warn


def _read_raw_pos(sid: int, addr_h: int) -> Optional[int]:
    """Driver's own signed 32-bit position at addr_h/addr_h+1, high word first, no offset."""
    regs = _read(sid, addr_h, 2)
    if regs is None:
        return None
    u = (regs[0] << 16) | regs[1]
    return u - 0x1_0000_0000 if u >= 0x8000_0000 else u


def _rezero_in_place(name: str, cfg: dict) -> bool:
    """While holding current is still 0, makes each driver of this axis
    accept where the axis was left by hand. The closed-loop driver keeps
    its commanded position through manual mode, so restoring current
    would otherwise drive the axis straight back to it. Writing 0x21 to
    0x6002 (manual set-zero) resets both the actual and commanded position
    to 0 at the current point; _pos_offset_pulses then keeps the app's
    coordinates where they were. Returns False if a motor couldn't be
    read or re-zeroed (its current should not be restored)."""
    ok = True
    for sid in cfg["ids"]:
        before = _read_raw_pos(sid, REG_ENC_POS_H)
        if before is None:
            _log(f"  [manual mode] {name} (id {sid}) position read failed — can't re-zero")
            ok = False
            continue
        app_pulses = before + _pos_offset_pulses.get(sid, 0)
        if not _write(sid, TRIGGER, TRIG_SET_ZERO):
            _log(f"  [manual mode] {name} (id {sid}) set-zero write failed")
            ok = False
            continue
        time.sleep(0.1)
        actual = _read_raw_pos(sid, REG_ENC_POS_H)
        command = _read_raw_pos(sid, REG_PROFILE_POS_H)
        if actual is None:
            _log(f"  [manual mode] {name} (id {sid}) position read after set-zero failed")
            ok = False
            continue
        # Anything the axis moved during the 0.1 s is still in actual, so
        # the offset is taken from the reading before the zero.
        _pos_offset_pulses[sid] = app_pulses
        mm_now = ((actual + app_pulses) / cfg["ppr"]) * cfg["mpr"]
        if command is None:
            _log(f"  [manual mode] {name} (id {sid}) re-zeroed at {mm_now:.2f} mm (commanded position unreadable)")
        elif abs(command - actual) > REZERO_MATCH_PULSES:
            _log(f"  [manual mode] {name} (id {sid}) WARNING — after re-zero the commanded position is "
                 f"{command - actual:+d} pulses from actual; it may still move when current is restored")
        else:
            _log(f"  [manual mode] {name} (id {sid}) re-zeroed at {mm_now:.2f} mm — commanded = actual")
    return ok


@app.post("/manual_mode/enter")
def manual_mode_enter():
    """Drops holding current to 0% on x/z/y so the axes can be moved by
    hand. REG_MOTOR_ENABLE (Pr0.07) can't actually force-disable this driver
    — confirmed against the Leadshine iCS-RS series manual: DI1 is a
    normally-closed enable input left unwired on this rig (so it's
    permanently asserted), and Pr0.07 only ever forces *toward* enable.
    Holding current is the real lever. Encoder position keeps updating
    (read_positions/_read_encoder_mm don't depend on either register), so
    the on-screen position stays accurate while you move things manually.
    """
    global _manual_mode
    if not _connected:
        _log("[manual mode] enter refused — not connected")
        return _err("not connected")
    if _is_busy():
        _log("[manual mode] enter refused — busy")
        return _err("busy")
    _log("[manual mode] entering — dropping holding current to 0% on x/z/y...")
    ok = True
    for name, cfg in MOTORS.items():
        if not cfg.get("enabled", True):
            _log(f"  [manual mode] {name} disabled — skipping")
            continue
        saved_per_id = {}
        for sid in cfg["ids"]:
            before = _read(sid, REG_HOLDING_CURRENT_PCT)
            saved = before[0] if before is not None else DEFAULT_HOLDING_CURRENT_PCT
            saved_per_id[sid] = saved
            if _set_motor_holding_current(sid, 0):
                _log(f"  [manual mode] {name} (id {sid}) holding current -> 0%, confirmed (was {saved}%)")
            else:
                ok = False
                _log(f"  [manual mode] {name} (id {sid}) holding-current drop FAILED")
        _manual_mode_saved_current[name] = saved_per_id
    # Spin and TubeGrip can be disabled outright (same as zeon_cherrypicker.py's release torque)
    for label, fn in (("spin", lambda: _spin_set_enabled(False)),
                      ("tubegrip", lambda: _tubegrip_set_enabled(False))):
        if fn():
            _log(f"  [manual mode] {label} released (free to move by hand)")
        else:
            ok = False
            _log(f"  [manual mode] {label} release FAILED")
    _manual_mode = True
    _log_op("manual mode: holding current dropped to 0%")
    return _ok() if ok else _err("one or more motors failed to drop holding current")


@app.post("/manual_mode/exit")
def manual_mode_exit():
    """Re-zeroes each driver where its axis was left (see _rezero_in_place,
    so it doesn't jump back to where it was before manual mode), restores
    each motor's holding current to its pre-manual-mode value, then clears
    any alarm — a following-error alarm (Pr0.05) can trip if the axis was
    hand-moved a long way while holding current was near zero.
    """
    global _manual_mode
    if not _connected:
        _log("[manual mode] exit refused — not connected")
        return _err("not connected")
    _log("[manual mode] exiting — restoring holding current on x/z/y...")
    ok = True
    for name, cfg in MOTORS.items():
        if not cfg.get("enabled", True):
            _log(f"  [manual mode] {name} disabled — skipping")
            continue
        if not _rezero_in_place(name, cfg):
            ok = False
            _log(f"  [manual mode] {name} not re-zeroed — leaving its holding current at 0%")
            continue
        saved_per_id = _manual_mode_saved_current.get(name, {})
        for sid in cfg["ids"]:
            target = saved_per_id.get(sid, DEFAULT_HOLDING_CURRENT_PCT)
            if _set_motor_holding_current(sid, target):
                _log(f"  [manual mode] {name} (id {sid}) holding current -> {target}%, confirmed")
            else:
                ok = False
                _log(f"  [manual mode] {name} (id {sid}) holding-current restore FAILED")
        _clear_alarm(cfg["ids"], cfg)
    for label, fn in (("spin", lambda: _spin_set_enabled(True)),
                      ("tubegrip", lambda: _tubegrip_set_enabled(True))):
        if fn():
            _log(f"  [manual mode] {label} holding torque restored")
        else:
            ok = False
            _log(f"  [manual mode] {label} enable FAILED")
    # TubeGrip's jaws may have been moved by hand — its reference is gone
    for key in ("measured_open_range", "measured_close_range", "open_direction_sign", "zero_ref"):
        GRIPPER_TUBEGRIP[key] = None
    _log("  [manual mode] tubegrip needs homing again before open/close")
    if ok:
        _manual_mode = False
        _log_op("manual mode: holding current restored")
        return _ok()
    # Leave _manual_mode True (still blocks automated motion) — clearing it
    # here would let automation run while a motor is confirmed to still be
    # at 0% holding current, which is unsafe rather than just inconvenient.
    _log_op("manual mode: holding current restore FAILED for one or more motors")
    return _err("one or more motors failed to restore holding current — still in manual mode")


@app.post("/gripper/open")
def gripper_open():
    err = _motion_refused()
    if err:
        return _err(err)
    ok = _gripper_move(GRIPPER_CMD_OPEN)
    _log(f"  [gripper] {'open' if ok else 'open FAILED'} — diag: {_gripper_servo_diag()}")
    _log_op("gripper open")
    return _ok() if ok else _err("gripper open failed")


@app.post("/gripper/close")
def gripper_close():
    err = _motion_refused()
    if err:
        return _err(err)
    ok = _gripper_move(GRIPPER_CMD_CLOSE)
    _log(f"  [gripper] {'close' if ok else 'close FAILED'} — diag: {_gripper_servo_diag()}")
    _log_op("gripper close")
    return _ok() if ok else _err("gripper close failed")


@app.post("/gripper/diag")
def gripper_diag_endpoint():
    """Read-only — see _gripper_servo_diag."""
    if not _connected:
        return _err("not connected")
    return _ok(diag=_gripper_servo_diag())


class GripperMoveReq(BaseModel):
    percent: float  # 0 = fully open, 100 = fully closed

@app.post("/gripper/moveto")
def gripper_moveto(req: GripperMoveReq):
    """The Basegrip protocol (see _gripper_move) has no arbitrary
    target-position command — only open and close — so this snaps to whichever discrete action the requested percent is
    closer to (< 50 -> open, >= 50 -> close) rather than silently doing
    nothing or driving a target register the hardware doesn't have.
    """
    err = _motion_refused()
    if err:
        return _err(err)
    pct = max(0.0, min(100.0, req.percent))
    cmd = GRIPPER_CMD_OPEN if pct < 50.0 else GRIPPER_CMD_CLOSE
    ok = _gripper_move(cmd)
    _log(f"  [gripper] move to {pct:.0f}% -> {'open' if cmd == GRIPPER_CMD_OPEN else 'close'} "
         f"(no intermediate positions on this hardware) {'ok' if ok else 'FAILED'}")
    _log_op(f"gripper {pct:.0f}%")
    return _ok() if ok else _err("gripper move failed")


# ── Spinning Gripper endpoints (TubeGrip grip + Spin rotation) ────────────────
# Independent gripper, always available alongside the static one — not a
# mode switch. Own URL namespace so both can be driven at the same time.

class TubegripMoveReq(BaseModel):
    percent: float  # 0 = fully open, 100 = fully closed


class TubegripSetTargetReq(BaseModel):
    percent: float


@app.post("/gripper/tubegrip/open")
def tubegrip_open_endpoint():
    """Moves to the saved "open" target (GRIPPER_TUBEGRIP["open_percent"],
    default 0% = fully open) via the confirmed relative-move mechanism —
    see _tubegrip_moveto_percent. No longer a collision-home crash: that
    action (with its torque/offset parameters) is still what
    /gripper/tubegrip/home uses internally to establish the reference, but
    isn't what the everyday open/close buttons do anymore. Change the
    target with /gripper/tubegrip/set_open_percent.
    """
    err = _motion_refused()
    if err:
        return _err(err)
    ok, reason, info = _tubegrip_moveto_percent_autohome(GRIPPER_TUBEGRIP["open_percent"], "tubegrip open")
    if ok:
        _log(f"  [tubegrip] open (target={GRIPPER_TUBEGRIP['open_percent']:.0f}%) "
             f"({info['start']} -> {info['final']}) settled={info['settled']}, diag: {info['diag']}")
        return _ok(**info)
    _log(f"  [tubegrip] open FAILED — {reason}")
    return _err(f"tubegrip open failed: {reason}")


@app.post("/gripper/tubegrip/close")
def tubegrip_close_endpoint():
    """Same as tubegrip_open_endpoint but moves to the saved "close" target
    (GRIPPER_TUBEGRIP["close_percent"], default 100% = fully closed).
    """
    err = _motion_refused()
    if err:
        return _err(err)
    ok, reason, info = _tubegrip_moveto_percent_autohome(GRIPPER_TUBEGRIP["close_percent"], "tubegrip close")
    if ok:
        _log(f"  [tubegrip] close (target={GRIPPER_TUBEGRIP['close_percent']:.0f}%) "
             f"({info['start']} -> {info['final']}) settled={info['settled']}, diag: {info['diag']}")
        return _ok(**info)
    _log(f"  [tubegrip] close FAILED — {reason}")
    return _err(f"tubegrip close failed: {reason}")


@app.post("/gripper/tubegrip/set_open_percent")
def tubegrip_set_open_percent(req: TubegripSetTargetReq):
    """Saves what the "open" button moves to — persisted to
    TUBEGRIP_TARGETS_FILE so it survives an app restart. Doesn't move
    anything itself; pair with /gripper/tubegrip/moveto (the live percent
    control) to get TubeGrip to the width you want first.
    """
    pct = max(0.0, min(100.0, req.percent))
    GRIPPER_TUBEGRIP["open_percent"] = pct
    _save_tubegrip_targets()
    _log(f"  [tubegrip] open target set to {pct:.0f}%")
    return _ok(open_percent=pct)


@app.post("/gripper/tubegrip/set_close_percent")
def tubegrip_set_close_percent(req: TubegripSetTargetReq):
    """Same as set_open_percent but for the "close" button's target."""
    pct = max(0.0, min(100.0, req.percent))
    GRIPPER_TUBEGRIP["close_percent"] = pct
    _save_tubegrip_targets()
    _log(f"  [tubegrip] close target set to {pct:.0f}%")
    return _ok(close_percent=pct)


@app.post("/gripper/tubegrip/moveto")
def tubegrip_moveto(req: TubegripMoveReq):
    """Percent-of-range move, 0=fully open, 100=fully closed, via the
    confirmed-working relative-move register (0x00DE) against the
    empirically measured open-side stroke — see _tubegrip_moveto_percent.
    Requires a prior /gripper/tubegrip/home (or /close then /measure_open)
    to establish the reference and range; direct position triggers
    (0x00D0/0x00E8) still don't reliably move this hardware, which is why
    this doesn't just target GRIPPER_TUBEGRIP's open_pos/close_pos config.
    """
    err = _motion_refused()
    if err:
        return _err(err)
    pct = max(0.0, min(100.0, req.percent))
    ok, reason, info = _tubegrip_moveto_percent(pct)
    if ok:
        _log(f"  [tubegrip] move to {pct:.0f}% (target={info['target']}) "
             f"({info['start']} -> {info['final']}) settled={info['settled']}, diag: {info['diag']}")
        return _ok(**info)
    _log(f"  [tubegrip] move to {pct:.0f}% FAILED — {reason}")
    return _err(f"tubegrip move failed: {reason}")


@app.post("/gripper/tubegrip/measure_open")
def tubegrip_measure_open_endpoint():
    """Opens TubeGrip and reports the peak pulse count observed during the
    motion — read-only observation, doesn't intervene in the move. Run this
    right after a normal close (position ~0) to measure the full open-side
    stroke length in pulses.
    """
    err = _motion_refused()
    if err:
        return _err(err)
    was_closed = GRIPPER_TUBEGRIP.get("zero_ref") == "closed"
    peak, peak_signed, reason = _tubegrip_measure_open()
    if peak is None:
        _log(f"  [tubegrip] measure open FAILED — {reason}")
        return _err(f"measure open failed: {reason}")
    GRIPPER_TUBEGRIP["measured_open_range"] = peak
    GRIPPER_TUBEGRIP["zero_ref"] = "open"
    if was_closed:
        GRIPPER_TUBEGRIP["open_direction_sign"] = 1 if peak_signed >= 0 else -1
    _log(f"  [tubegrip] measured open range: {peak} pulses (signed={peak_signed}, "
         f"direction {'confirmed' if was_closed else 'NOT confirmed — not measured from a closed=0 reference'})")
    return _ok(peak=peak)


@app.post("/gripper/tubegrip/measure_close")
def tubegrip_measure_close_endpoint():
    """Same as measure_open but for close."""
    err = _motion_refused()
    if err:
        return _err(err)
    peak, peak_signed, reason = _tubegrip_measure_close()
    if peak is None:
        _log(f"  [tubegrip] measure close FAILED — {reason}")
        return _err(f"measure close failed: {reason}")
    GRIPPER_TUBEGRIP["measured_close_range"] = peak
    GRIPPER_TUBEGRIP["zero_ref"] = "closed"
    _log(f"  [tubegrip] measured close range: {peak} pulses")
    return _ok(peak=peak)


def _tubegrip_home_sequence() -> tuple[bool, str, dict]:
    """Homing sequence: open fully (unconditional starting state) → measure
    close (establishes a clean position-0 reference) → measure open
    (measures the full open-side stroke, and its raw-encoder sign, from
    that reference) → close again (leaves zero_ref == "closed"). Same safe,
    read-only observation as the individual measure endpoints — just runs
    all four in order so homing always starts from a known state regardless
    of wherever TubeGrip happened to be beforehand, AND always finishes with
    a valid "closed" reference. Required once before /gripper/tubegrip/moveto
    (and open/close, which now move via percent rather than crashing
    directly) will run.

    The final close is not optional: _tubegrip_moveto_percent falls back to
    a real collision-home crash whenever zero_ref isn't already "closed" —
    if home left zero_ref == "open" (as it used to), the very next open/close
    button press would silently trigger a surprise crash-close before doing
    the move the user actually asked for, defeating the whole point of the
    2026-08-12 port away from crash-on-every-click (see
    feedback_gripper_dev_workflow memory). Paying for that reference-establish
    crash once here, during home, is expected; deferring it into an
    unrelated button press is not.

    Plain function (not the endpoint itself) so it can also run from
    do_home's "home all" and from /connect — see callers.
    """
    # The positive soft position limit (0x0070) has been observed on
    # hardware coming up stuck at a stray tight value (e.g. 5 pulses)
    # rather than its documented factory default. That only gates
    # position-mode moves (moveto/open/close), not the collision-home
    # crashes this sequence itself uses — so a stuck limit doesn't make
    # homing fail, it just silently no-ops every open/close button press
    # afterward (target commanded, position never actually changes) until
    # something clears it. Clear it unconditionally at the start of every
    # home so that can't happen again without a code failure being logged
    # (best-effort: doesn't abort homing if the write itself fails, since
    # the collision-home steps below don't depend on it).
    # A TubeGrip left in alarm (often after the script restarts) ignores
    # every move, so clear that first — a driver restart, no power cycle.
    if not _tubegrip_reset_if_alarmed("tubegrip home"):
        return False, "TubeGrip is in alarm or not responding (see log)", {}
    _tubegrip_clear_stuck_move("tubegrip home")

    limit_ok, limit_reason = _tubegrip_clear_soft_pos_limit()
    if limit_ok:
        _log("  [tubegrip] home: soft position limit cleared")
    else:
        _log(f"  [tubegrip] home: WARNING — could not clear soft position limit ({limit_reason}); "
             f"open/close may silently no-op until /gripper/tubegrip/clear_soft_pos_limit is run")

    _log("  [tubegrip] home: opening fully first...")
    initial_peak, _initial_signed, initial_reason = _tubegrip_measure_open()
    if initial_peak is None:
        _log(f"  [tubegrip] home FAILED at initial open — {initial_reason}")
        return False, f"home failed at initial open: {initial_reason}", {}
    GRIPPER_TUBEGRIP["zero_ref"] = "open"
    _log("  [tubegrip] home: opened fully, closing...")
    close_peak, _close_signed, close_reason = _tubegrip_measure_moved(_tubegrip_measure_close)
    if close_peak is None:
        _log(f"  [tubegrip] home FAILED at close — {close_reason}")
        return False, f"home failed at close: {close_reason}", {}
    GRIPPER_TUBEGRIP["measured_close_range"] = close_peak
    GRIPPER_TUBEGRIP["zero_ref"] = "closed"
    _log(f"  [tubegrip] home: closed (measured {close_peak} pulses), opening...")
    open_peak, open_signed, open_reason = _tubegrip_measure_moved(_tubegrip_measure_open)
    if open_peak is None:
        _log(f"  [tubegrip] home FAILED at open — {open_reason}")
        return False, f"home failed at open: {open_reason}", {}
    # This open measurement runs immediately after the close above, so 0 is
    # a known closed reference here — the sign is trustworthy.
    GRIPPER_TUBEGRIP["measured_open_range"] = open_peak
    GRIPPER_TUBEGRIP["open_direction_sign"] = 1 if open_signed >= 0 else -1
    GRIPPER_TUBEGRIP["zero_ref"] = "open"
    _log("  [tubegrip] home: measured open, closing to finish with a valid reference...")
    final_close_peak, _final_close_signed, final_close_reason = _tubegrip_measure_moved(_tubegrip_measure_close)
    if final_close_peak is None:
        _log(f"  [tubegrip] home FAILED at final close — {final_close_reason}")
        return False, f"home failed at final close: {final_close_reason}", {}
    GRIPPER_TUBEGRIP["measured_close_range"] = final_close_peak
    GRIPPER_TUBEGRIP["zero_ref"] = "closed"
    _log(f"  [tubegrip] home complete — close={final_close_peak}, open={open_peak} pulses "
         f"(open direction sign={GRIPPER_TUBEGRIP['open_direction_sign']})")
    return True, "", {"close_peak": final_close_peak, "open_peak": open_peak}


@app.post("/gripper/tubegrip/home")
def tubegrip_home():
    err = _motion_refused()
    if err:
        return _err(err)
    ok, reason, info = _tubegrip_home_sequence()
    return _ok(**info) if ok else _err(reason)


@app.post("/gripper/tubegrip/clear_soft_pos_limit")
def tubegrip_clear_soft_pos_limit_endpoint():
    """Resets the positive software position limit (0x0070) to its
    documented factory default — see _tubegrip_clear_soft_pos_limit. Only
    needed once per power-cycle if the driver came up with a stray tight
    value (confirmed on hardware: 5, origin unknown, not written by any
    code in this project).
    """
    if not _connected:
        return _err("not connected")
    ok, reason = _tubegrip_clear_soft_pos_limit()
    if ok:
        _log("  [tubegrip] soft position limit cleared")
        return _ok()
    _log(f"  [tubegrip] clear soft pos limit FAILED — {reason}")
    return _err(f"clear soft pos limit failed: {reason}")


@app.post("/gripper/tubegrip/enable")
def tubegrip_enable():
    """Hold torque (re-enable)."""
    if not _connected:
        return _err("not connected")
    ok = _tubegrip_set_enabled(True)
    _log(f"  [tubegrip] {'enabled (holding torque)' if ok else 'enable FAILED'}")
    return _ok() if ok else _err("tubegrip enable failed")


@app.post("/gripper/tubegrip/disable")
def tubegrip_disable():
    """Release windings so the gripper can be moved by hand."""
    if not _connected:
        return _err("not connected")
    ok = _tubegrip_set_enabled(False)
    _log(f"  [tubegrip] {'disabled (free to move by hand)' if ok else 'disable FAILED'}")
    return _ok() if ok else _err("tubegrip disable failed")


def _spin_set_enabled(enabled: bool) -> bool:
    """Same as _tubegrip_set_enabled, for Spin: enable (hold torque) or
    release the windings so the collar can be rotated by hand, via 0x00D4.
    Note _spin_move always re-enables (writes 0x00D4=0) before moving, so
    any Spin move ends a hand-rotate session.
    """
    sid = GRIPPER_SPIN["id"]
    if not enabled:
        _write(sid, GRIPPER_REG_RUN_STOP, GRIPPER_CMD_STOP)
    return _write(sid, GRIPPER_REG_ENABLE, 0x0000 if enabled else 0x0001)


@app.post("/gripper/spin/enable")
def spin_enable():
    """Hold torque (re-enable)."""
    if not _connected:
        return _err("not connected")
    ok = _spin_set_enabled(True)
    _log(f"  [spin] {'enabled (holding torque)' if ok else 'enable FAILED'}")
    return _ok() if ok else _err("spin enable failed")


@app.post("/gripper/spin/disable")
def spin_disable():
    """Release windings so Spin can be rotated by hand."""
    if not _connected:
        return _err("not connected")
    ok = _spin_set_enabled(False)
    _log(f"  [spin] {'disabled (free to rotate by hand)' if ok else 'disable FAILED'}")
    return _ok() if ok else _err("spin disable failed")


@app.post("/gripper/tubegrip/clear_alarm")
def tubegrip_clear_alarm_endpoint():
    """Clears a latched alarm by restarting TubeGrip's driver (see
    _tubegrip_reset_if_alarmed), then reports the alarm state. Never writes
    the destructive 0x00A4 clear register. After a restart TubeGrip needs a
    home before open/close work again."""
    err = _motion_refused()
    if err:
        return _err(err)
    _tubegrip_reset_if_alarmed("tubegrip clear alarm")
    ok, result = _tubegrip_clear_alarm()
    if ok:
        _log(f"  [tubegrip] clear alarm — current alarm: {result}")
        return _ok(alarm=result, homed=GRIPPER_TUBEGRIP["measured_open_range"] is not None)
    _log(f"  [tubegrip] clear alarm FAILED — {result}")
    return _err(f"clear alarm failed: {result}")


class SpinMoveReq(BaseModel):
    percent: float  # 0 = fully open, 100 = fully closed


class SpinAngleReq(BaseModel):
    angle_deg: float  # absolute target angle, relative to the position-0 reference


class SpinPositionsReq(BaseModel):
    open_pos: int        # encoder pulses (GRIPPER_SPIN_COUNTS_PER_REV per turn)
    close_pos: int
    cap_holder_pos: int = 0


@app.post("/gripper/spin/open")
def spin_open():
    err = _motion_refused()
    if err:
        return _err(err)
    ok = _spin_move(GRIPPER_SPIN["open_pos"])
    _log(f"  [spin] {'open' if ok else 'open FAILED'}")
    return _ok() if ok else _err("spin open failed")


@app.post("/gripper/spin/close")
def spin_close():
    err = _motion_refused()
    if err:
        return _err(err)
    ok = _spin_move(GRIPPER_SPIN["close_pos"])
    _log(f"  [spin] {'close' if ok else 'close FAILED'}")
    return _ok() if ok else _err("spin close failed")


@app.post("/gripper/spin/moveto")
def spin_moveto(req: SpinMoveReq):
    err = _motion_refused()
    if err:
        return _err(err)
    pct = max(0.0, min(100.0, req.percent))
    pos = int(GRIPPER_SPIN["open_pos"] + (GRIPPER_SPIN["close_pos"] - GRIPPER_SPIN["open_pos"]) * pct / 100.0)
    ok = _spin_move(pos)
    _log(f"  [spin] move to {pct:.0f}% (pos={pos}) {'ok' if ok else 'FAILED'}")
    return _ok() if ok else _err("spin move failed")


@app.post("/gripper/spin/moveto_angle")
def spin_moveto_angle(req: SpinAngleReq):
    """Rotates Spin to an absolute angle (degrees, relative to the
    position-0 reference), converted to pulses via GRIPPER_SPIN_COUNTS_PER_REV.
    Spin is a continuous rotator, so an angle target is more meaningful than
    percent-of-open/close-range.
    """
    err = _motion_refused()
    if err:
        return _err(err)
    pos = int(round(req.angle_deg / 360.0 * GRIPPER_SPIN_COUNTS_PER_REV))
    ok = _spin_move(pos)
    _log(f"  [spin] rotate to {req.angle_deg:g}° (pos={pos}) {'ok' if ok else 'FAILED'}")
    return _ok(pos=pos) if ok else _err("spin move failed")


@app.post("/gripper/spin/positions")
def spin_set_positions(req: SpinPositionsReq):
    """Config-only — doesn't touch the bus. Sets open/close/cap holder as
    raw encoder pulse positions (the same units as the live position
    readout, so a position found by rotating Spin by hand can be saved
    exactly). Persisted to calibration.json as spin_*_pos so it
    survives an app restart; the *_rev values are kept only as a derived
    readout. Replaces the older revolution-based spin_*_rev keys, which
    are dropped from the file on save (still read once on load if present).
    """
    _save_spin_positions(req.open_pos, req.close_pos, req.cap_holder_pos)
    _log(f"  [spin] positions set — open={req.open_pos}, close={req.close_pos}, "
         f"cap holder={req.cap_holder_pos} pulses")
    return _ok(open_pos=req.open_pos, close_pos=req.close_pos, cap_holder_pos=req.cap_holder_pos)


def _save_spin_positions(open_pos: int, close_pos: int, cap_holder_pos: int) -> None:
    global _calibration
    _set_spin_positions(open_pos, close_pos, cap_holder_pos)
    _calibration["spin_open_pos"] = int(open_pos)
    _calibration["spin_close_pos"] = int(close_pos)
    _calibration["spin_cap_holder_pos"] = int(cap_holder_pos)
    for legacy in ("spin_open_rev", "spin_close_rev", "spin_cap_holder_rev"):
        _calibration.pop(legacy, None)
    _save_calibration(_calibration)


@app.post("/gripper/spin/set_current/{which}")
def spin_set_current(which: str):
    """Reads Spin's live encoder position from the driver and saves it as
    the open, close or cap holder position — for releasing torque, twisting
    Spin by hand to the right spot, and capturing it in one click. Reads the
    driver directly rather than trusting the page's last live readout.
    """
    if which not in ("open", "close", "cap_holder"):
        return _err(f"unknown position '{which}'")
    if not _connected:
        return _err("not connected")
    pos = _read_int32(GRIPPER_SPIN["id"], 0x0004)
    if pos is None:
        return _err("spin position read failed")
    current = {n: GRIPPER_SPIN[f"{n}_pos"] for n in ("open", "close", "cap_holder")}
    current[which] = pos
    _save_spin_positions(current["open"], current["close"], current["cap_holder"])
    _log(f"  [spin] {which.replace('_', ' ')} position set to current: {pos} pulses")
    return _ok(which=which, pos=pos)


class GrabReq(BaseModel):
    z_mm: float

class PlaceReq(BaseModel):
    z_mm: float

@app.post("/grab")
def do_grab(req: GrabReq):
    if not _connected:
        return _err("not connected")
    if _is_busy():
        return _err("busy")
    def _task():
        _log(f"  [grab] opening tubegrip")
        if not _tubegrip_open_step("grab", auto_home=True): return
        if _stop_flag.is_set(): return
        _log(f"  [grab] lowering z_gripper to {req.z_mm} mm")
        if not extend_to("z_gripper", req.z_mm):
            _log("  [grab] z_gripper extend failed — aborting")
            return
        if _stop_flag.is_set(): return
        _log("  [grab] gripping tubegrip")
        if not _tubegrip_grab_step("grab"): return
        if _stop_flag.is_set(): return
        _log("  [grab] raising z_gripper")
        home("z_gripper")
    _log_op(f"grab z={req.z_mm}mm")
    if not _run_bg(_task):
        return _err(_bg_refusal())
    return _ok(started=True)

@app.post("/place")
def do_place(req: PlaceReq):
    if not _connected:
        return _err("not connected")
    if _is_busy():
        return _err("busy")
    def _task():
        _log(f"  [place] lowering z_gripper to {req.z_mm} mm")
        if not extend_to("z_gripper", req.z_mm):
            _log("  [place] z_gripper extend failed — aborting")
            return
        if _stop_flag.is_set(): return
        _log("  [place] opening tubegrip")
        if not _tubegrip_open_step("place"): return
        if _stop_flag.is_set(): return
        _log("  [place] raising z_gripper")
        home("z_gripper")
    _log_op(f"place z={req.z_mm}mm")
    if not _run_bg(_task):
        return _err(_bg_refusal())
    return _ok(started=True)


class DecapReq(BaseModel):
    z_mm: float

def _move_xy_blocking(x: float, y: float) -> None:
    """Moves x and y in parallel and waits for both — same pattern already
    used by test_well/move_tube/tube_loop, factored out so /decap and
    /recap can move to their preset positions too.
    """
    threads = []
    for name, target in [("y", y), ("x", x)]:
        def _m(n=name, t=target): extend_to(n, t)
        th = threading.Thread(target=_m, daemon=True)
        threads.append(th); th.start()
    for th in threads: th.join()


@app.post("/decap")
def do_decap(req: DecapReq):
    if not _connected:
        return _err("not connected")
    if _is_busy():
        return _err("busy")
    def _task():
        holder = _calibration.get("tube_holder")
        dx, dy = (holder["x"], holder["y"]) if holder else (None, None)
        if dx is not None and dy is not None:
            if not _home_all_z("decap"): return
            if _stop_flag.is_set(): return
            _log(f"  [decap] moving to preset position x={dx} y={dy}")
            _move_xy_blocking(dx, dy)
            if _stop_flag.is_set(): return
            _log("  [decap] x+y confirmed — settling")
            time.sleep(pause_time)
            if _stop_flag.is_set(): return
        _log("  [decap] closing rotation gripper")
        if not _spin_move(GRIPPER_SPIN["close_pos"]):
            _log("  [decap] spin close failed — aborting")
            return
        if _stop_flag.is_set(): return
        _log("  [decap] opening tubegrip")
        if not _tubegrip_open_step("decap", auto_home=True): return
        if _stop_flag.is_set(): return
        _log(f"  [decap] lowering z_gripper to {req.z_mm} mm")
        if not extend_to("z_gripper", req.z_mm):
            _log("  [decap] z_gripper extend failed — aborting")
            return
        if _stop_flag.is_set(): return
        # Real collision-home grip, not a percent move — this needs to
        # actually clamp onto the cap, same reasoning as do_grab's
        # gripping step (see _tubegrip_grab_step).
        _log("  [decap] gripping tubegrip")
        if not _tubegrip_grab_step("decap"): return
        if _stop_flag.is_set(): return
        _log("  [decap] opening rotation gripper")
        if not _spin_move(GRIPPER_SPIN["open_pos"]):
            _log("  [decap] spin open failed — aborting")
            return
        if _stop_flag.is_set(): return
        _log("  [decap] raising z_gripper")
        home("z_gripper")
    _log_op(f"decap z={req.z_mm}mm")
    if not _run_bg(_task):
        return _err(_bg_refusal())
    return _ok(started=True)


class RecapReq(BaseModel):
    z_mm: float

@app.post("/recap")
def do_recap(req: RecapReq):
    if not _connected:
        return _err("not connected")
    if _is_busy():
        return _err("busy")
    def _task():
        holder = _calibration.get("tube_holder")
        rx, ry = (holder["x"], holder["y"]) if holder else (None, None)
        if rx is not None and ry is not None:
            if not _home_all_z("recap"): return
            if _stop_flag.is_set(): return
            _log(f"  [recap] moving to preset position x={rx} y={ry}")
            _move_xy_blocking(rx, ry)
            if _stop_flag.is_set(): return
            _log("  [recap] x+y confirmed — settling")
            time.sleep(pause_time)
            if _stop_flag.is_set(): return
        _log(f"  [recap] lowering z_gripper to {req.z_mm} mm")
        if not extend_to("z_gripper", req.z_mm):
            _log("  [recap] z_gripper extend failed — aborting")
            return
        if _stop_flag.is_set(): return
        _log("  [recap] closing rotation gripper")
        if not _spin_move(GRIPPER_SPIN["close_pos"]):
            _log("  [recap] spin close failed — aborting")
            return
        if _stop_flag.is_set(): return
        _log("  [recap] opening tubegrip")
        if not _tubegrip_open_step("recap"): return
        if _stop_flag.is_set(): return
        _log("  [recap] raising z_gripper")
        home("z_gripper")
    _log_op(f"recap z={req.z_mm}mm")
    if not _run_bg(_task):
        return _err(_bg_refusal())
    return _ok(started=True)


class TestWellReq(BaseModel):
    x: float
    y: float
    grab_z_mm: float
    place_z_mm: float

@app.post("/test_well")
def do_test_well(req: TestWellReq):
    if not _connected:
        return _err("not connected")
    if _is_busy():
        return _err("busy")

    def _task():
        # open tubegrip and home both z axes before any x/y move
        _log("  [test well] opening tubegrip")
        if not _tubegrip_open_step("test well", auto_home=True): return
        if _stop_flag.is_set(): return
        if not _home_all_z_for_plate_move("test well"): return
        if _stop_flag.is_set(): return

        # move x and y to well in parallel
        threads = []
        for name, target in [("y", req.y), ("x", req.x)]:
            def _move(n=name, t=target): extend_to(n, t)
            th = threading.Thread(target=_move, daemon=True)
            threads.append(th)
            th.start()
        for th in threads: th.join()
        if _stop_flag.is_set(): return
        _log("  [test well] x+y confirmed — settling 3 s")
        time.sleep(pause_time)
        if _stop_flag.is_set(): return

        # grab
        _log(f"  [test well] grab — lowering z_gripper to {req.grab_z_mm} mm")
        if not extend_to("z_gripper", req.grab_z_mm):
            _log("  [test well] z_gripper extend failed — aborting")
            return
        if _stop_flag.is_set(): return
        _log("  [test well] gripping tubegrip")
        if not _tubegrip_grab_step("test well"): return
        if _stop_flag.is_set(): return
        _log("  [test well] raising z_gripper")
        if not home("z_gripper"): return
        if _stop_flag.is_set(): return

        # place
        _log(f"  [test well] place — lowering z_gripper to {req.place_z_mm} mm")
        if not extend_to("z_gripper", req.place_z_mm):
            _log("  [test well] z_gripper extend failed — aborting")
            return
        if _stop_flag.is_set(): return
        _log("  [test well] opening tubegrip")
        if not _tubegrip_open_step("test well"): return
        if _stop_flag.is_set(): return
        _log("  [test well] raising z_gripper")
        home("z_gripper")
        _log("  [test well] complete")

    _log_op(f"test well x={req.x} y={req.y}")
    if not _run_bg(_task):
        return _err(_bg_refusal())
    return _ok(started=True)


class MoveTubeReq(BaseModel):
    from_x: float
    from_y: float
    to_x: float
    to_y: float
    grab_z_mm: float
    place_z_mm: float

@app.post("/move_tube")
def do_move_tube(req: MoveTubeReq):
    err = _static_gripper_refused()
    if err:
        return _err(err)
    if not _connected:
        return _err("not connected")
    if _is_busy():
        return _err("busy")

    def _move_xy(x, y):
        threads = []
        for name, target in [("y", y), ("x", x)]:
            def _m(n=name, t=target): extend_to(n, t)
            th = threading.Thread(target=_m, daemon=True)
            threads.append(th)
            th.start()
        for th in threads: th.join()

    def _task():
        # open gripper and home both z axes before any x/y move
        _log("  [move tube] opening gripper")
        _gripper_move(GRIPPER_CMD_OPEN)
        if _stop_flag.is_set(): return
        if not _home_all_z_for_plate_move("move tube"): return
        if _stop_flag.is_set(): return

        # move x+y to source well
        _log(f"  [move tube] moving to source x={req.from_x} y={req.from_y}")
        _move_xy(req.from_x, req.from_y)
        if _stop_flag.is_set(): return
        _log("  [move tube] x+y confirmed — settling 3 s")
        time.sleep(pause_time)
        if _stop_flag.is_set(): return

        # grab
        _log(f"  [move tube] lowering z_gripper to {req.grab_z_mm} mm")
        if not extend_to("z_gripper", req.grab_z_mm): return
        if _stop_flag.is_set(): return
        _log("  [move tube] closing gripper")
        _gripper_move(GRIPPER_CMD_CLOSE)
        if _stop_flag.is_set(): return
        if not _home_all_z_for_plate_move("move tube"): return
        if _stop_flag.is_set(): return

        # move x+y to destination (gripper stays closed)
        _log(f"  [move tube] moving to destination x={req.to_x} y={req.to_y}")
        _move_xy(req.to_x, req.to_y)
        if _stop_flag.is_set(): return
        _log("  [move tube] x+y confirmed — settling 3 s")
        time.sleep(pause_time)
        if _stop_flag.is_set(): return

        # place
        _log(f"  [move tube] lowering z_gripper to {req.place_z_mm} mm")
        if not extend_to("z_gripper", req.place_z_mm): return
        if _stop_flag.is_set(): return
        _log("  [move tube] opening gripper")
        _gripper_move(GRIPPER_CMD_OPEN)
        if _stop_flag.is_set(): return
        _log("  [move tube] raising z_gripper")
        home("z_gripper")
        _log("  [move tube] complete")

    _log_op(f"move tube ({req.from_x},{req.from_y}) → ({req.to_x},{req.to_y})")
    if not _run_bg(_task):
        return _err(_bg_refusal())
    return _ok(started=True)


SPIN_OPEN_RISE_MM = 6.0   # how far z_gripper rises while the spinning gripper opens (decap tube, showcase decap)

def _spin_open_with_rise(label: str, z_from_mm: float) -> bool:
    """Opens the spinning gripper and, at the same time, raises z_gripper by
    SPIN_OPEN_RISE_MM from z_from_mm (smaller mm = higher). z's
    speed is set for this one move so the rise takes about as long as the
    spin (from Spin's distance and speed_rpm), then put back."""
    cfg = MOTORS["z_gripper"]
    z_to = max(0.0, z_from_mm - SPIN_OPEN_RISE_MM)
    start = _read_int32(GRIPPER_SPIN["id"], 0x0004)
    if start is None:
        start = GRIPPER_SPIN["close_pos"]
    counts = abs(GRIPPER_SPIN["open_pos"] - start)
    spin_s = counts / (GRIPPER_SPIN["speed_rpm"] * GRIPPER_SPIN_COUNTS_PER_REV / 60.0)
    rise_rev = (z_from_mm - z_to) / cfg["mpr"]
    z_rpm = cfg["rpm"] if spin_s <= 0 else max(5, min(cfg["rpm"], round(rise_rev / spin_s * 60)))

    _log(f"  [{label}] opening spinning gripper while raising z_gripper "
         f"{z_from_mm:.1f} → {z_to:.1f} mm at {z_rpm} rpm (spin ≈ {spin_s:.2f} s)")
    z_result = {}
    normal_rpm = cfg["rpm"]
    cfg["rpm"] = z_rpm
    try:
        z_thread = threading.Thread(
            target=lambda: z_result.setdefault("ok", extend_to("z_gripper", z_to)), daemon=True)
        z_thread.start()
        spin_ok = _spin_move(GRIPPER_SPIN["open_pos"])
        z_thread.join()
    finally:
        cfg["rpm"] = normal_rpm
    if not spin_ok:
        _log(f"  [{label}] spinning gripper open FAILED — aborting")
        return False
    if not z_result.get("ok"):
        _log(f"  [{label}] z_gripper rise FAILED — aborting")
        return False
    return True


SHOWCASE_PARK_Z_MM = 95.0   # z_gripper height the cap is held at while the user works on the open tube
SHOWCASE_PARK_X_MM = 5.0    # x the cap is carried to, out of the user's way

# Showcase decap is driven by the user in three button presses, so its step
# lives here between requests rather than inside one background task:
#   "idle"          → begin button
#   "running"       → a step is in progress (no buttons)
#   "await_finish"  → tube is open, cap parked at x=SHOWCASE_PARK_X_MM → finished button
#   "await_release" → tube recapped, still clamped in the static gripper → release tube button
# A step that fails or is stopped falls through to "await_release", since
# the tube is clamped in the static gripper and the user needs a way to get
# it out.
_showcase_phase = "idle"


def _showcase_move_xyz(name, pos, home_z: bool = True) -> bool:
    """Moves x/y to pos, then lowers z_gripper to pos["z"]. Homes the z axes
    first unless home_z is False — only for when z_gripper is known to be
    at park height (SHOWCASE_PARK_Z_MM), which clears everything x/y pass
    over between the park position and the tube holder."""
    if home_z and not _home_all_z("showcase decap"):
        return False
    if _stop_flag.is_set(): return False
    _log(f"  [showcase decap] moving to {name} — x={pos['x']} y={pos['y']}")
    threads = [threading.Thread(target=extend_to, args=(n, pos[n]), daemon=True) for n in ("y", "x")]
    for th in threads: th.start()
    for th in threads: th.join()
    if _stop_flag.is_set(): return False
    _log(f"  [showcase decap] lowering z_gripper to {pos['z']} mm at {name}")
    if not extend_to("z_gripper", pos["z"]):
        _log("  [showcase decap] z_gripper extend failed — aborting")
        return False
    return True


def _showcase_regrip() -> bool:
    """Closes tubegrip on the cap straight after the open that set it down.
    _tubegrip_grab_step alone isn't enough here: fired right after an open,
    the grab can be dropped while the driver is still finishing the open
    move, and its settle check (position unchanged for 3 reads) then passes
    immediately on jaws that never moved — so the spinning gripper would
    close with nothing holding the cap. This waits for the driver to go
    idle first, checks the jaws actually moved, and retries the grab once
    if they didn't."""
    sid = GRIPPER_TUBEGRIP["id"]
    for attempt in (1, 2):
        t0 = time.monotonic()
        while _tubegrip_run_state(sid) not in (0, None) and time.monotonic() - t0 < 3.0:
            time.sleep(0.05)
        # Still "running" with the jaws open and empty → a stuck move that
        # would swallow the grab; cancel it first.
        _tubegrip_clear_stuck_move("showcase decap")
        time.sleep(0.2)
        start = _read_int32(sid, 0x0004)
        if not _tubegrip_grab_step("showcase decap"):
            return False
        end = _read_int32(sid, 0x0004)
        # A real grab re-zeros at the stall point, so after one the reading
        # is near 0 and well away from the open position it started at.
        if start is None or end is None or abs(end - start) > 50:
            return True
        _log(f"  [showcase decap] tubegrip didn't move on grab attempt {attempt} "
             f"(pos {start} → {end}; {_tubegrip_diag(sid)})")
    _log("  [showcase decap] tubegrip regrip FAILED — aborting before recap")
    return False


def _showcase_start(expect: str, task, next_phase: str):
    """Shared checks + runner for the three showcase buttons. Runs task in
    the background; on success moves to next_phase, otherwise to
    "await_release" so the tube can still be released."""
    global _showcase_phase
    if not _connected:
        return _err("not connected")
    if _is_busy():
        return _err("busy")
    if _showcase_phase != expect:
        return _err(f"showcase decap is not waiting for that step (currently {_showcase_phase})")

    def _wrapped():
        global _showcase_phase
        ok = False
        try:
            ok = task() is True and not _stop_flag.is_set()
        finally:
            _showcase_phase = next_phase if ok else "await_release"
            if not ok:
                _log("  [showcase decap] stopped — press release tube to open the static gripper")

    prev = _showcase_phase
    _showcase_phase = "running"
    if not _run_bg(_wrapped):
        _showcase_phase = prev
        return _err("busy")
    return _ok(started=True)


def _showcase_begin_task(holder) -> bool:
    """Showcase step 1 — the tube has been placed by hand in the tube holder
    (static gripper).
      0. move to the park position if not already there, and home TubeGrip
         there (_showcase_home_tubegrip) — before the tube is clamped, so a
         failed home leaves nothing to release
      1. close the static gripper on the tube
      2. open tubegrip
      3. close the spinning gripper
      4. move to the tube holder x/y at park height, then lower z_gripper
         (no z home — step 0 left z_gripper parked)
      5. close tubegrip on the cap
      6. open the spinning gripper while z_gripper rises SPIN_OPEN_RISE_MM
         (see _spin_open_with_rise) — unscrews the cap
      7. raise z_gripper to SHOWCASE_PARK_Z_MM (no homing), then move x to
         SHOWCASE_PARK_X_MM, carrying the cap out of the way
    """
    if not _showcase_home_tubegrip(): return False
    if _stop_flag.is_set(): return False

    _log("  [showcase decap] closing static gripper on the tube")
    if not _gripper_move(GRIPPER_CMD_CLOSE):
        _log("  [showcase decap] static gripper close FAILED — aborting")
        return False
    if _stop_flag.is_set(): return False

    _log("  [showcase decap] opening tubegrip")
    if not _tubegrip_open_step("showcase decap", auto_home=True): return False
    if _stop_flag.is_set(): return False

    _log("  [showcase decap] closing spinning gripper")
    if not _spin_move(GRIPPER_SPIN["close_pos"]):
        _log("  [showcase decap] spinning gripper close FAILED — aborting")
        return False
    if _stop_flag.is_set(): return False

    # z_gripper is still at park height from _showcase_home_tubegrip (step
    # 0) — the spin/static gripper steps since then don't move it — so x/y
    # travel there without homing z first.
    if not _showcase_move_xyz("static gripper", holder, home_z=False): return False
    if _stop_flag.is_set(): return False

    _log("  [showcase decap] closing tubegrip — grabbing cap")
    if not _tubegrip_grab_step("showcase decap"): return False
    if _stop_flag.is_set(): return False

    if not _spin_open_with_rise("showcase decap", holder["z"]): return False
    if _stop_flag.is_set(): return False

    return _showcase_park()


def _showcase_finish_task(holder, recap_depth) -> bool:
    """Showcase step 2 — recaps the open tube.
      1. move x back to the tube holder x (z_gripper stays at
         SHOWCASE_PARK_Z_MM, the height it left at)
      2. lower z_gripper to the recap height (recap_z_mm)
      3. open tubegrip — sets the cap down on the tube
      4. close tubegrip on the cap again (see _showcase_regrip)
      5. close the spinning gripper — screws the cap back on
      6. open tubegrip — lets go of the cap so raising z doesn't pull the
         tube out of the static gripper
      7. raise z_gripper to SHOWCASE_PARK_Z_MM (no homing), then move x to
         SHOWCASE_PARK_X_MM, out of the way of the tube
    """
    _log(f"  [showcase decap] moving x back to the static gripper — x={holder['x']}")
    threads = [threading.Thread(target=extend_to, args=(n, holder[n]), daemon=True) for n in ("y", "x")]
    for th in threads: th.start()
    for th in threads: th.join()
    if _stop_flag.is_set(): return False

    _log(f"  [showcase decap] lowering z_gripper to recap height {recap_depth} mm")
    if not extend_to("z_gripper", recap_depth):
        _log("  [showcase decap] z_gripper extend failed — aborting")
        return False
    if _stop_flag.is_set(): return False

    _log("  [showcase decap] opening tubegrip — setting cap down on the tube")
    if not _tubegrip_open_step("showcase decap"): return False
    if _stop_flag.is_set(): return False

    _log("  [showcase decap] closing tubegrip — regripping cap")
    if not _showcase_regrip(): return False
    if _stop_flag.is_set(): return False

    _log("  [showcase decap] closing spinning gripper — screwing cap on")
    if not _spin_move(GRIPPER_SPIN["close_pos"]):
        _log("  [showcase decap] spinning gripper close FAILED — aborting")
        return False
    if _stop_flag.is_set(): return False

    _log("  [showcase decap] opening tubegrip — releasing cap")
    if not _tubegrip_open_step("showcase decap"): return False
    if _stop_flag.is_set(): return False

    return _showcase_park()


def _showcase_park() -> bool:
    # z first, then x — x never moves with z_gripper still down at the tube
    _log(f"  [showcase decap] raising z_gripper to {SHOWCASE_PARK_Z_MM} mm")
    if not extend_to("z_gripper", SHOWCASE_PARK_Z_MM):
        _log("  [showcase decap] z_gripper move failed — aborting")
        return False
    if _stop_flag.is_set(): return False
    _log(f"  [showcase decap] moving x to {SHOWCASE_PARK_X_MM} mm")
    if not extend_to("x", SHOWCASE_PARK_X_MM):
        _log("  [showcase decap] x move failed — aborting")
        return False
    return True


def _showcase_release_task() -> bool:
    """Showcase step 3 — opens the static gripper so the tube can be taken
    out. TubeGrip is not homed here; the next begin homes it at park."""
    if _static_gripper_disabled():
        _log("  [showcase decap] static gripper disabled — nothing to release")
        return True
    _log("  [showcase decap] opening static gripper — releasing tube")
    if not _gripper_move(GRIPPER_CMD_OPEN):
        _log("  [showcase decap] static gripper open FAILED")
        return False
    return True


SHOWCASE_PARK_TOL_MM = 0.5   # how close x/z must be to the park position to count as parked


def _showcase_go_to_park() -> bool:
    """Moves to the park position (x=SHOWCASE_PARK_X_MM, z_gripper=
    SHOWCASE_PARK_Z_MM) unless already there. x never moves with z_gripper
    below park height: z rises to park height first (or homes, if it isn't
    homed), then x moves, then z settles at park height."""
    pos = read_positions()
    x = (pos.get("x") or {}).get("val")
    z = (pos.get("z_gripper") or {}).get("val")
    if (x is not None and z is not None and _is_homed.get("z_gripper")
            and abs(x - SHOWCASE_PARK_X_MM) <= SHOWCASE_PARK_TOL_MM
            and abs(z - SHOWCASE_PARK_Z_MM) <= SHOWCASE_PARK_TOL_MM):
        return True
    _log(f"  [showcase decap] moving to park position (x={SHOWCASE_PARK_X_MM}, z={SHOWCASE_PARK_Z_MM}) "
         f"to home tubegrip — now x={x}, z={z}")
    if z is None or not _is_homed.get("z_gripper"):
        if not _home_all_z("showcase decap"): return False
    elif z > SHOWCASE_PARK_Z_MM + SHOWCASE_PARK_TOL_MM:   # larger mm = lower
        _log(f"  [showcase decap] raising z_gripper to {SHOWCASE_PARK_Z_MM} mm")
        if not extend_to("z_gripper", SHOWCASE_PARK_Z_MM):
            _log("  [showcase decap] z_gripper move failed — aborting")
            return False
    if _stop_flag.is_set(): return False
    _log(f"  [showcase decap] moving x to {SHOWCASE_PARK_X_MM} mm")
    if not extend_to("x", SHOWCASE_PARK_X_MM):
        _log("  [showcase decap] x move failed — aborting")
        return False
    if _stop_flag.is_set(): return False
    _log(f"  [showcase decap] lowering z_gripper to park height {SHOWCASE_PARK_Z_MM} mm")
    if not extend_to("z_gripper", SHOWCASE_PARK_Z_MM):
        _log("  [showcase decap] z_gripper move failed — aborting")
        return False
    return True


def _showcase_home_tubegrip() -> bool:
    """Homes TubeGrip at the start and end of every showcase decap (and so
    every loop decap cycle): re-measures its range and clears a latched
    alarm or a stuck move (see _tubegrip_home_sequence) so each decap
    starts from a known TubeGrip. Always at the park position — moves there
    first if needed (_showcase_go_to_park). Must only run while TubeGrip
    holds nothing — homing opens and closes it fully."""
    if not _showcase_go_to_park(): return False
    if _stop_flag.is_set(): return False
    _log("  [showcase decap] homing tubegrip at park position")
    ok, reason, _info = _tubegrip_home_sequence()
    if not ok and not _stop_flag.is_set():
        # Nothing is held and nothing else moves here, so one more try is
        # safe — a driver just released from a stuck move or alarm can miss
        # the first home's commands.
        _log(f"  [showcase decap] tubegrip home failed ({reason}) — trying once more")
        time.sleep(1.0)
        ok, reason, _info = _tubegrip_home_sequence()
    if not ok:
        _log(f"  [showcase decap] tubegrip home FAILED — {reason}")
    return ok


def _showcase_calibration():
    """(holder, recap_depth, error) — read fresh each time so a recap height
    changed in the Control tab applies to the next step/cycle."""
    holder = _calibration.get("tube_holder")
    if holder is None:
        return None, None, "static gripper position not calibrated yet — set it in the tube holder tab first"
    recap_depth = _calibration.get("recap_z_mm")
    if recap_depth is None:
        return None, None, "recap height not set — set it in the Control tab first"
    return holder, recap_depth, None


@app.post("/showcase_decap/begin")
def do_showcase_begin():
    """Button 1 — see _showcase_begin_task. Then waits for finished."""
    err = _static_gripper_refused()
    if err:
        return _err(err)
    holder, _recap, error = _showcase_calibration()
    if error:
        return _err(error)

    def _task():
        if not _showcase_begin_task(holder): return False
        _log("  [showcase decap] tube is open — press finished when done")
        return True

    _log_op("showcase decap begin")
    return _showcase_start("idle", _task, "await_finish")


@app.post("/showcase_decap/finish")
def do_showcase_finish():
    """Button 2 — see _showcase_finish_task. Then waits for release tube."""
    holder, recap_depth, error = _showcase_calibration()
    if error:
        return _err(error)

    def _task():
        if not _showcase_finish_task(holder, recap_depth): return False
        _log("  [showcase decap] tube recapped — press release tube to open the static gripper")
        return True

    _log_op("showcase decap finish")
    return _showcase_start("await_finish", _task, "await_release")


@app.post("/showcase_decap/release")
def do_showcase_release():
    """Button 3 — opens the static gripper and goes back to idle."""
    def _task():
        if not _showcase_release_task(): return False
        _log("  [showcase decap] done")
        return True

    _log_op("showcase decap release")
    return _showcase_start("await_release", _task, "idle")


class ShowcaseLoopReq(BaseModel):
    cycles: Optional[int] = None   # None = loop until stop is pressed


_showcase_loop_progress: Optional[str] = None   # e.g. "3 / 10" while the decap loop runs, for the UI
_showcase_loop_last: Optional[str] = None       # e.g. "7 cycles completed" once the last loop ended


@app.post("/showcase_decap/loop")
def do_showcase_loop(req: ShowcaseLoopReq):
    """Runs the whole showcase decap — begin, finish, release — back to back
    with no button presses, req.cycles times or until stop. The tube stays
    in the tube holder between cycles: release opens the static gripper and
    the next begin closes it again. Stopping mid-cycle leaves the release
    tube button, same as a stopped single run."""
    err = _static_gripper_refused()
    if err:
        return _err(err)
    _holder, _recap, error = _showcase_calibration()
    if error:
        return _err(error)
    if req.cycles is not None and req.cycles < 1:
        return _err("cycles must be at least 1, or blank to loop until stop")
    total = "∞" if req.cycles is None else str(req.cycles)

    def _task():
        global _showcase_loop_progress, _showcase_loop_last
        n = 0
        done = 0
        try:
            while req.cycles is None or n < req.cycles:
                n += 1
                _showcase_loop_progress = f"{n} / {total}"
                _log(f"  [loop decap] cycle {n} / {total}")
                holder, recap_depth, error = _showcase_calibration()
                if error:
                    _log(f"  [loop decap] {error}")
                    return False
                if not _showcase_begin_task(holder): return False
                if not _showcase_finish_task(holder, recap_depth): return False
                if not _showcase_release_task(): return False
                done = n
                if _stop_flag.is_set(): return False
            _log(f"  [loop decap] done — {n} cycles")
            return True
        finally:
            _showcase_loop_progress = None
            _showcase_loop_last = f"{done} / {total} cycles completed"

    _log_op(f"loop decap {total} cycles")
    return _showcase_start("idle", _task, "idle")


class DecapTubeReq(BaseModel):
    plate: str
    well: str

@app.post("/decap_tube")
def do_decap_tube(req: DecapTubeReq):
    """Grabs a single tube from a plate well with TubeGrip, hands it off to
    the static gripper at the tube holder, decaps it, stores the cap in the
    cap holder, then returns the (now capless) tube to its original well:
      1. close the rotation (decapper/GRIPPER_SPIN) gripper — done before
         tubegrip even opens, so it's in a known, closed starting state
      2. open tubegrip
      3. move to the tube (well position) — lowers z_gripper to grab_z_mm
         first, same calibrated grab depth used everywhere else a tube is
         picked out of a well (not explicitly listed as its own step, but
         TubeGrip can't reach the tube without it)
      4. close tubegrip — grips the tube (by its cap)
      5. open the static gripper
      6. move the tube to the static gripper (tube holder position) —
         z_gripper retracts first (standard safety rule: never move x/y
         with a z axis extended), then re-lowers to the calibrated
         decap_z_mm depth at the holder (same height used by the manual
         "decap" action in the Control tab), same reasoning as step 3:
         the static gripper can't close on the tube without it being at
         the right height
      7. close the static gripper — tubegrip is NOT released here; both
         grippers end up holding the tube (static gripper on the tube
         body, tubegrip on the cap)
      8. spin the decapper gripper (GRIPPER_SPIN) open — unscrews the cap;
         tubegrip is left holding just the (now loose) cap, while the
         static gripper keeps the tube body fixed at the holder. z_gripper
         rises SPIN_OPEN_RISE_MM at the same time, paced to the
         spin (see _spin_open_with_rise)
      9. home z — lifts tubegrip (and the cap it's holding) clear of the
         tube, which stays behind in the static gripper
     10. rotate the spinning gripper (GRIPPER_SPIN) to the calibrated cap
         holder angle — the cap is still held by tubegrip at this point,
         so this presents it at a consistent, programmable orientation
         before it's carried to the holder
     11. move the cap to the next open position in the cap holder (first
         calibrated holder plate with a free slot) and lower z to that
         position's calibrated depth
     12. open tubegrip — deposits the cap in the holder; that position is
         marked occupied
     13. home z again before heading back (same x/y-with-z-extended rule
         as step 6)
     14. move back to the tube holder's full calibrated x/y/z, then lower
         z an additional relative +5 mm to meet the tube
     15. close tubegrip — regrips the (capless) tube
     16. open the static gripper ("tube holder") — releases the tube, now
         held by tubegrip alone
     17. home z, then move the tube back to its original well on the tube
         plate and release it there (mirrors the pickup in steps 3-4)
    """
    err = _static_gripper_refused()
    if err:
        return _err(err)
    if not _connected:
        return _err("not connected")
    if _is_busy():
        return _err("busy")

    plate_data = _calibration.get("plates", {}).get(req.plate)
    if plate_data is None:
        return _err(f"unknown plate '{req.plate}'")
    if _plate_corners(plate_data, _PLATE_DEFAULT_GRID) is None:
        return _err(f"calibrate '{req.plate}' first — all 4 corners, or one corner with pitch set")
    src_pos = _well_pos(plate_data, req.well)
    if src_pos is None:
        return _err(f"invalid well '{req.well}'")

    holder_pos = _calibration.get("tube_holder")
    if holder_pos is None:
        return _err("tube holder position not calibrated yet — set it in the Tube Holder tab first")

    grab_depth = _calibration.get("grab_z_mm")
    if grab_depth is None:
        return _err("grab height not set — set it in the Control tab first")
    holder_depth = _calibration.get("decap_z_mm")
    if holder_depth is None:
        return _err("decap height not set — set it in the Control tab first")

    open_slot = _next_open_holder_slot()
    if open_slot is None:
        return _err("no open cap holder position — calibrate a cap holder plate or clear some positions first")
    cap_plate, cap_pos_id, cap_xyz = open_slot

    def _move_xy(x, y):
        threads = []
        for name, target in [("y", y), ("x", x)]:
            def _m(n=name, t=target): extend_to(n, t)
            th = threading.Thread(target=_m, daemon=True)
            threads.append(th); th.start()
        for th in threads: th.join()

    def _task():
        # 1. close the rotation (decapper) gripper first
        _log("  [decap tube] closing rotation gripper")
        if not _spin_move(GRIPPER_SPIN["close_pos"]):
            _log("  [decap tube] rotation gripper close FAILED — aborting")
            return
        if _stop_flag.is_set(): return

        # 2. open tubegrip
        _log("  [decap tube] opening tubegrip")
        if not _tubegrip_open_step("decap tube", auto_home=True): return
        if _stop_flag.is_set(): return

        # 3. move to the tube
        if not _home_all_z_for_plate_move("decap tube"): return
        if _stop_flag.is_set(): return
        _log(f"  [decap tube] moving to {req.plate}/{req.well} — x={src_pos['x']} y={src_pos['y']}")
        _move_xy(src_pos["x"], src_pos["y"])
        if _stop_flag.is_set(): return
        _log("  [decap tube] x+y confirmed — settling 3 s")
        time.sleep(pause_time)
        if _stop_flag.is_set(): return
        _log(f"  [decap tube] lowering z_gripper to {grab_depth} mm to reach tube")
        if not extend_to("z_gripper", grab_depth):
            _log("  [decap tube] z_gripper extend failed — aborting")
            return
        if _stop_flag.is_set(): return

        # 4. close tubegrip
        _log("  [decap tube] closing tubegrip — grabbing tube")
        if not _tubegrip_grab_step("decap tube"): return
        if _stop_flag.is_set(): return

        # 5. open the static gripper
        _log("  [decap tube] opening static gripper")
        _gripper_move(GRIPPER_CMD_OPEN)
        if _stop_flag.is_set(): return

        # 6. move the tube to the static gripper (tube holder) — _home_all_z
        # already homes/raises z_gripper (and z_pipettor) as part of its own
        # loop, so no separate standalone home() call is needed here; that
        # used to run the full crash-to-limit-switch cycle twice in a row.
        if not _home_all_z("decap tube"): return
        if _stop_flag.is_set(): return
        _log(f"  [decap tube] moving to tube holder — x={holder_pos['x']} y={holder_pos['y']}")
        _move_xy(holder_pos["x"], holder_pos["y"])
        if _stop_flag.is_set(): return
        _log("  [decap tube] x+y confirmed")
        _log(f"  [decap tube] lowering z_gripper to {holder_depth} mm at tube holder")
        if not extend_to("z_gripper", holder_depth):
            _log("  [decap tube] z_gripper extend failed — aborting")
            return
        if _stop_flag.is_set(): return

        # 7. close the static gripper
        _log("  [decap tube] closing static gripper")
        if not _gripper_move(GRIPPER_CMD_CLOSE):
            _log("  [decap tube] static gripper close FAILED — aborting")
            return
        if _stop_flag.is_set(): return

        # 8. spin the decapper gripper open, raising z as the cap unscrews
        if not _spin_open_with_rise("decap tube", holder_depth):
            return
        if _stop_flag.is_set(): return

        # 9. home z — lifts tubegrip (holding the cap) clear of the tube,
        # which stays behind in the (still closed) static gripper
        if not _home_all_z("decap tube"): return
        if _stop_flag.is_set(): return

        # 10. rotate the spinning gripper to the cap holder angle before
        # approaching the cap holder — the cap is still held by tubegrip,
        # so this presents it at a consistent, programmable orientation
        # for the holder slot.
        _log(f"  [decap tube] rotating to cap holder position ({GRIPPER_SPIN['cap_holder_pos']} pulses)")
        if not _spin_move(GRIPPER_SPIN["cap_holder_pos"]):
            _log("  [decap tube] cap holder angle rotate FAILED — aborting")
            return
        if _stop_flag.is_set(): return

        # 11. move the cap to the next open cap holder position
        _log(f"  [decap tube] moving cap to holder '{cap_plate}' position {cap_pos_id} — "
             f"x={cap_xyz['x']} y={cap_xyz['y']}")
        _move_xy(cap_xyz["x"], cap_xyz["y"])
        if _stop_flag.is_set(): return
        _log("  [decap tube] x+y confirmed")
        _log(f"  [decap tube] lowering z_gripper to {cap_xyz['z']} mm at cap holder")
        if not extend_to("z_gripper", cap_xyz["z"]):
            _log("  [decap tube] z_gripper extend failed — aborting")
            return
        if _stop_flag.is_set(): return

        # 12. open tubegrip — deposits the cap
        _log("  [decap tube] opening tubegrip — releasing cap in holder")
        if not _tubegrip_open_step("decap tube"): return
        if _stop_flag.is_set(): return
        occupied = set(_calibration["holder_plates"].get(cap_plate, {}).get("caps", []))
        occupied.add(cap_pos_id)
        _calibration["holder_plates"].setdefault(cap_plate, {})["caps"] = list(occupied)
        _save_calibration(_calibration)

        # 13. home z before heading back to the tube holder
        if not _home_all_z("decap tube"): return
        if _stop_flag.is_set(): return

        # 14. move back to the tube holder's full calibrated x/y/z, then an
        # additional relative +5mm to meet the (still-held) tube
        _log(f"  [decap tube] moving to tube holder — x={holder_pos['x']} y={holder_pos['y']}")
        _move_xy(holder_pos["x"], holder_pos["y"])
        if _stop_flag.is_set(): return
        _log("  [decap tube] x+y confirmed")
        _log(f"  [decap tube] lowering z_gripper to tube holder z={holder_pos['z']} mm")
        if not extend_to("z_gripper", holder_pos["z"]):
            _log("  [decap tube] z_gripper extend failed — aborting")
            return
        if _stop_flag.is_set(): return
        regrip_depth = holder_pos["z"] + 2.0
        _log(f"  [decap tube] lowering z_gripper an additional 5mm to {regrip_depth} mm")
        if not extend_to("z_gripper", regrip_depth):
            _log("  [decap tube] z_gripper extend failed — aborting")
            return
        if _stop_flag.is_set(): return

        # 15. close tubegrip — regrips the capless tube
        _log("  [decap tube] closing tubegrip — regripping tube")
        if not _tubegrip_grab_step("decap tube"): return
        if _stop_flag.is_set(): return

        # 16. open the static gripper ("tube holder") — tubegrip alone
        # holds the tube now
        _log("  [decap tube] opening static gripper")
        _gripper_move(GRIPPER_CMD_OPEN)
        if _stop_flag.is_set(): return

        # 17. move the tube back to its original well
        if not _home_all_z_for_plate_move("decap tube"): return
        if _stop_flag.is_set(): return
        _log(f"  [decap tube] moving tube back to {req.plate}/{req.well} — x={src_pos['x']} y={src_pos['y']}")
        _move_xy(src_pos["x"], src_pos["y"])
        if _stop_flag.is_set(): return
        _log("  [decap tube] x+y confirmed — settling 3 s")
        time.sleep(pause_time)
        if _stop_flag.is_set(): return
        _log(f"  [decap tube] lowering z_gripper to {grab_depth} mm to place tube")
        if not extend_to("z_gripper", grab_depth):
            _log("  [decap tube] z_gripper extend failed — aborting")
            return
        if _stop_flag.is_set(): return
        _log("  [decap tube] opening tubegrip — releasing tube")
        if not _tubegrip_open_step("decap tube"): return
        if _stop_flag.is_set(): return
        _log("  [decap tube] raising z_gripper")
        home("z_gripper")
        _log("  [decap tube] complete")

    _log_op(f"decap tube {req.plate}/{req.well}")
    if not _run_bg(_task):
        return _err(_bg_refusal())
    return _ok(started=True)


# ── Calibration ───────────────────────────────────────────────────────────────

def _load_calibration() -> dict:
    if CALIBRATION_FILE.exists():
        try:
            data = json.loads(CALIBRATION_FILE.read_text())
            # migrate: A12 at root → single plate with corners
            if "A12" in data and "plates" not in data:
                data["plates"] = {"Plate 1": {"corners": {"A12": data.pop("A12")}}}
                CALIBRATION_FILE.write_text(json.dumps(data, indent=2))
            if "plates" not in data:
                data["plates"] = {"Plate 1": {}}
            if "holder_plates" not in data:
                data["holder_plates"] = {"Holder 1": {}}
            # migrate per-plate: plate.A12 → plate.corners.A12
            dirty = False
            for plate_data in data["plates"].values():
                if "A12" in plate_data and "corners" not in plate_data:
                    plate_data["corners"] = {"A12": plate_data.pop("A12")}
                    dirty = True
                if "corners" not in plate_data:
                    plate_data["corners"] = {}
            for plate_data in data["holder_plates"].values():
                if "corners" not in plate_data:
                    plate_data["corners"] = {}
                    dirty = True
            if dirty:
                CALIBRATION_FILE.write_text(json.dumps(data, indent=2))
            return data
        except Exception:
            pass
    return {"plates": {"Plate 1": {"corners": {}}}, "holder_plates": {"Holder 1": {"corners": {}}}}

def _save_calibration(data: dict) -> None:
    CALIBRATION_FILE.write_text(json.dumps(data, indent=2))

_calibration: dict = _load_calibration()
# Spin positions are saved as pulses (spin_*_pos). Older files saved
# revolutions (spin_*_rev) — converted here so existing setups carry over
# until the next save rewrites them as pulses.
def _saved_spin_pos(name: str) -> Optional[int]:
    if f"spin_{name}_pos" in _calibration:
        return int(_calibration[f"spin_{name}_pos"])
    if f"spin_{name}_rev" in _calibration:
        return int(round(_calibration[f"spin_{name}_rev"] * GRIPPER_SPIN_COUNTS_PER_REV))
    return None

_set_spin_positions(*(
    p if p is not None else GRIPPER_SPIN[f"{n}_pos"]
    for n, p in ((n, _saved_spin_pos(n)) for n in ("open", "close", "cap_holder"))
))


# ── TubeGrip open/close percent targets ─────────────────────────────────────
# What the "open"/"close" buttons move TubeGrip to (percent of the measured
# range — see _tubegrip_moveto_percent), as opposed to the live percent
# input/"go to" control which always addresses the full 0-100 range as-is.
# User-settable via "set as open"/"set as close" (captures whatever's
# currently in the live percent control) and persisted here so the choice
# survives an app restart. Note: this does NOT persist the physical
# reference itself (measured_open_range/open_direction_sign/zero_ref) —
# those reset on restart and still require a fresh /gripper/tubegrip/home
# before "open"/"close" (or moveto) will move anything, same as before.

def _load_tubegrip_targets() -> dict:
    if TUBEGRIP_TARGETS_FILE.exists():
        try:
            data = json.loads(TUBEGRIP_TARGETS_FILE.read_text())
            return {
                "open_percent": float(data.get("open_percent", 0.0)),
                "close_percent": float(data.get("close_percent", 100.0)),
            }
        except Exception:
            pass
    return {"open_percent": 0.0, "close_percent": 100.0}

def _save_tubegrip_targets() -> None:
    TUBEGRIP_TARGETS_FILE.write_text(json.dumps({
        "open_percent": GRIPPER_TUBEGRIP["open_percent"],
        "close_percent": GRIPPER_TUBEGRIP["close_percent"],
    }, indent=2))

_tubegrip_targets = _load_tubegrip_targets()
GRIPPER_TUBEGRIP["open_percent"] = _tubegrip_targets["open_percent"]
GRIPPER_TUBEGRIP["close_percent"] = _tubegrip_targets["close_percent"]


class CalibSaveReq(BaseModel):
    plate: str
    corner: str   # one of the plate's 4 corners, e.g. A12, H12, A1, H1 for 12 rows x 8 cols
    x: float
    z: float
    y: float

class TubeHolderSaveReq(BaseModel):
    x: float
    z: float
    y: float

class AddPlateReq(BaseModel):
    plate: str
    rows: Optional[int] = None             # numbered 1..rows; default = old fixed size
    cols: Optional[int] = None             # lettered A..; default = old fixed size
    row_pitch_mm: Optional[float] = None   # spacing between rows (1 -> 2), mm
    col_pitch_mm: Optional[float] = None   # spacing between columns (A -> B), mm

class DeletePlateReq(BaseModel):
    plate: str

def _new_grid_plate(plates: dict, req: AddPlateReq, default: dict):
    """Validate an add-plate request and build the new plate's entry.
    Returns (plate_dict, None) or (None, error message)."""
    name = req.plate.strip()
    if not name:
        return None, "enter a plate name"
    if name in plates:
        return None, f"a plate called '{name}' already exists"
    rows = default["rows"] if req.rows is None else req.rows
    cols = default["cols"] if req.cols is None else req.cols
    if not 1 <= rows <= _GRID_MAX_ROWS:
        return None, f"rows must be 1-{_GRID_MAX_ROWS}"
    if not 1 <= cols <= _GRID_MAX_COLS:
        return None, f"columns must be 1-{_GRID_MAX_COLS} (one letter each)"
    for label, p in (("row pitch", req.row_pitch_mm), ("column pitch", req.col_pitch_mm)):
        if p is not None and not p > 0:
            return None, f"{label} must be more than 0 mm"
    return {"grid": {"rows": rows, "cols": cols,
                     "row_pitch_mm": req.row_pitch_mm, "col_pitch_mm": req.col_pitch_mm},
            "corners": {}}, None

@app.get("/calibration")
def get_calibration():
    return {"ok": True, "calibration": _calibration}

@app.post("/calibration/save")
def save_calibration(req: CalibSaveReq):
    global _calibration
    names = _grid_corner_names(_grid(_calibration["plates"].get(req.plate), _PLATE_DEFAULT_GRID))
    if req.corner not in names:
        return _err(f"invalid corner '{req.corner}' — must be one of {names}")
    plate = _calibration["plates"].setdefault(req.plate, {"corners": {}})
    plate.setdefault("corners", {})[req.corner] = {"x": req.x, "z": req.z, "y": req.y}
    _save_calibration(_calibration)
    _log(f"  [calibration] {req.plate} {req.corner} saved — x={req.x} z={req.z} y={req.y}")
    return {"ok": True, "calibration": _calibration}

@app.post("/calibration/save_tube_holder")
def save_tube_holder(req: TubeHolderSaveReq):
    global _calibration
    _calibration["tube_holder"] = {"x": req.x, "z": req.z, "y": req.y}
    _save_calibration(_calibration)
    _log(f"  [calibration] tube holder saved — x={req.x} z={req.z} y={req.y}")
    return {"ok": True, "calibration": _calibration}

@app.post("/calibration/save_pipetting_stand")
def save_pipetting_stand(req: TubeHolderSaveReq):
    global _calibration
    _calibration["pipetting_stand"] = {"x": req.x, "z": req.z, "y": req.y}
    _save_calibration(_calibration)
    _log(f"  [calibration] pipetting stand saved — x={req.x} z={req.z} y={req.y}")
    return {"ok": True, "calibration": _calibration}

@app.post("/calibration/add_plate")
def add_plate(req: AddPlateReq):
    global _calibration
    plate, err = _new_grid_plate(_calibration["plates"], req, _PLATE_DEFAULT_GRID)
    if err:
        return _err(err)
    _calibration["plates"][req.plate.strip()] = plate
    _save_calibration(_calibration)
    g = plate["grid"]
    _log(f"  [calibration] plate '{req.plate.strip()}' added — {g['rows']} rows x {g['cols']} columns, "
         f"pitch {g['row_pitch_mm']} x {g['col_pitch_mm']} mm")
    return {"ok": True, "calibration": _calibration}

@app.post("/calibration/delete_plate")
def delete_plate(req: DeletePlateReq):
    global _calibration
    plates = _calibration["plates"]
    if req.plate in plates:
        del plates[req.plate]
    if not plates:
        plates["Plate 1"] = {}
    _save_calibration(_calibration)
    return {"ok": True, "calibration": _calibration}


class HeightsSaveReq(BaseModel):
    grab_z_mm: float
    place_z_mm: float
    decap_z_mm: float = 60.0   # how far to lower z_gripper at the tube holder before decapping
    recap_z_mm: float = 60.0

@app.post("/calibration/save_heights")
def save_heights(req: HeightsSaveReq):
    global _calibration
    _calibration["grab_z_mm"]  = req.grab_z_mm
    _calibration["place_z_mm"] = req.place_z_mm
    _calibration["decap_z_mm"] = req.decap_z_mm
    _calibration["recap_z_mm"] = req.recap_z_mm
    _save_calibration(_calibration)
    return {"ok": True}



class SaveTubesReq(BaseModel):
    plate: str
    tubes: list

@app.post("/calibration/save_tubes")
def save_tubes(req: SaveTubesReq):
    global _calibration
    _calibration["plates"].setdefault(req.plate, {})["tubes"] = req.tubes
    _save_calibration(_calibration)
    return {"ok": True}


# ── Tube cap holder plates (own grid each; default 8 rows x 6 cols) ────────────────────────────────────
class HolderCalibSaveReq(BaseModel):
    plate: str
    corner: str   # one of the plate's 4 corners, e.g. A8, F8, A1, F1 for 8 rows x 6 cols
    x: float
    z: float
    y: float

class AddHolderPlateReq(AddPlateReq):
    pass

class DeleteHolderPlateReq(BaseModel):
    plate: str

class SaveHolderCapsReq(BaseModel):
    plate: str
    caps: list

@app.post("/calibration/holder_plate/save")
def save_holder_calibration(req: HolderCalibSaveReq):
    global _calibration
    names = _grid_corner_names(_grid(_calibration["holder_plates"].get(req.plate), _HOLDER_DEFAULT_GRID))
    if req.corner not in names:
        return _err(f"invalid corner '{req.corner}' — must be one of {names}")
    plate = _calibration["holder_plates"].setdefault(req.plate, {"corners": {}})
    plate.setdefault("corners", {})[req.corner] = {"x": req.x, "z": req.z, "y": req.y}
    _save_calibration(_calibration)
    _log(f"  [calibration] cap holder {req.plate} {req.corner} saved — x={req.x} z={req.z} y={req.y}")
    return {"ok": True, "calibration": _calibration}

@app.post("/calibration/holder_plate/add")
def add_holder_plate(req: AddHolderPlateReq):
    global _calibration
    plate, err = _new_grid_plate(_calibration["holder_plates"], req, _HOLDER_DEFAULT_GRID)
    if err:
        return _err(err)
    _calibration["holder_plates"][req.plate.strip()] = plate
    _save_calibration(_calibration)
    g = plate["grid"]
    _log(f"  [calibration] cap holder '{req.plate.strip()}' added — {g['rows']} rows x {g['cols']} columns, "
         f"pitch {g['row_pitch_mm']} x {g['col_pitch_mm']} mm")
    return {"ok": True, "calibration": _calibration}

@app.post("/calibration/holder_plate/delete")
def delete_holder_plate(req: DeleteHolderPlateReq):
    global _calibration
    plates = _calibration["holder_plates"]
    if req.plate in plates:
        del plates[req.plate]
    if not plates:
        plates["Holder 1"] = {}
    _save_calibration(_calibration)
    return {"ok": True, "calibration": _calibration}

@app.post("/calibration/holder_plate/save_caps")
def save_holder_caps(req: SaveHolderCapsReq):
    global _calibration
    _calibration["holder_plates"].setdefault(req.plate, {})["caps"] = req.caps
    _save_calibration(_calibration)
    return {"ok": True}


# ── Tube shuffle loop ─────────────────────────────────────────────────────────

class TubeLoopReq(BaseModel):
    plate: str = ""              # legacy single-plate field (still honored)
    plates: list = []            # shuffle across these plates; empty → [plate]
    grab_z_mm: float
    place_z_mm: float
    iterations: int = 0          # 0 = run until stopped
    allowed_wells: list = []     # empty = every well (applied to every plate)

@app.post("/tube_loop/start")
def tube_loop_start(req: TubeLoopReq):
    global _tube_loop_state
    if not _connected:
        return _err("not connected")
    if _is_busy():
        return _err("busy")

    plates = req.plates or ([req.plate] if req.plate else [])
    if not plates:
        return _err("no plates selected")
    for p in plates:
        if _plate_corners(_calibration["plates"].get(p, {}), _PLATE_DEFAULT_GRID) is None:
            return _err(f"calibrate '{p}' first — all 4 corners, or one corner with pitch set")

    def _move_xy(x, y):
        threads = []
        for name, target in [("y", y), ("x", x)]:
            def _m(n=name, t=target): extend_to(n, t)
            th = threading.Thread(target=_m, daemon=True)
            threads.append(th); th.start()
        for th in threads: th.join()

    def _task():
        global _tube_loop_state
        _tube_loop_state = {
            "running": True, "status": "starting",
            "moves_done": 0, "total": req.iterations,
            "last_from": None, "last_to": None,
        }
        count = 0
        try:
            while not _stop_flag.is_set():
                if req.iterations > 0 and count >= req.iterations:
                    break

                pool = set(req.allowed_wells) if req.allowed_wells else None

                # Build occupied / empty cells across ALL selected plates,
                # treating every (plate, well) as one shared pool so a tube can
                # be drawn from any plate and placed on any plate at random.
                occupied = []   # [(plate, well), ...]
                empty    = []
                calib_ok = True
                for p in plates:
                    plate_data = _calibration["plates"].get(p, {})
                    if _plate_corners(plate_data, _PLATE_DEFAULT_GRID) is None:
                        _log(f"  [tube loop] calibration for '{p}' lost — stopping")
                        calib_ok = False
                        break
                    tube_set = set(plate_data.get("tubes", []))
                    # the range applies to every plate, limited to the wells each plate has
                    plate_wells = _grid_positions(_grid(plate_data, _PLATE_DEFAULT_GRID))
                    for w in (plate_wells if pool is None else [w for w in plate_wells if w in pool]):
                        (occupied if w in tube_set else empty).append((p, w))
                if not calib_ok:
                    break

                if not occupied or not empty:
                    _log("  [tube loop] no valid moves in range — stopping")
                    break

                from_plate, from_well = random.choice(occupied)
                to_plate,   to_well   = random.choice(empty)
                from_pos = _well_pos(_calibration["plates"][from_plate], from_well)
                to_pos   = _well_pos(_calibration["plates"][to_plate],   to_well)

                _tube_loop_state["status"]    = f"move {count+1}: {from_plate}/{from_well} → {to_plate}/{to_well}"
                _tube_loop_state["last_from"] = f"{from_plate}/{from_well}"
                _tube_loop_state["last_to"]   = f"{to_plate}/{to_well}"
                _log(f"  [tube loop] {_tube_loop_state['status']}")

                # open tubegrip + home both z axes before any x/y move
                if not _tubegrip_open_step("tube loop", auto_home=True): break
                if _stop_flag.is_set(): break
                if not _home_all_z_for_plate_move("tube loop"): break
                if _stop_flag.is_set(): break

                # move to source
                _move_xy(from_pos["x"], from_pos["y"])
                if _stop_flag.is_set(): break
                _log("  [tube loop] x+y confirmed — settling 3 s")
                time.sleep(pause_time)
                if _stop_flag.is_set(): break

                # grab
                if not extend_to("z_gripper", req.grab_z_mm): break
                if _stop_flag.is_set(): break
                if not _tubegrip_grab_step("tube loop"): break
                if _stop_flag.is_set(): break
                if not _home_all_z_for_plate_move("tube loop"): break
                if _stop_flag.is_set(): break

                # move to destination (gripper closed)
                _move_xy(to_pos["x"], to_pos["y"])
                if _stop_flag.is_set(): break
                _log("  [tube loop] x+y confirmed — settling 3 s")
                time.sleep(pause_time)
                if _stop_flag.is_set(): break

                # place
                if not extend_to("z_gripper", req.place_z_mm): break
                if _stop_flag.is_set(): break
                if not _tubegrip_open_step("tube loop"): break
                if _stop_flag.is_set(): break
                home("z_gripper")

                # update tube maps on the source and destination plates
                src_set = set(_calibration["plates"][from_plate].get("tubes", []))
                src_set.discard(from_well)
                _calibration["plates"][from_plate]["tubes"] = list(src_set)
                # re-read dest (reflects the discard when src and dst are same plate)
                dst_set = set(_calibration["plates"][to_plate].get("tubes", []))
                dst_set.add(to_well)
                _calibration["plates"][to_plate]["tubes"] = list(dst_set)
                _save_calibration(_calibration)

                count += 1
                _tube_loop_state["moves_done"] = count

        finally:
            _tube_loop_state["running"] = False
            _tube_loop_state["status"]  = f"done — {count} move{'s' if count != 1 else ''}"
            _log(f"  [tube loop] finished — {count} moves")

    _log_op(f"tube loop start — plates={','.join(plates)} iterations={req.iterations or '∞'}")
    if not _run_bg(_task):
        return _err(_bg_refusal())
    return _ok(started=True)


@app.get("/tube_loop/state")
def tube_loop_state():
    # return loop metadata + current tubes per plate so frontend can sync
    plates_tubes = {
        name: data.get("tubes", [])
        for name, data in _calibration.get("plates", {}).items()
    }
    return _ok(state=_tube_loop_state, plates_tubes=plates_tubes)


# ── Laser distance sensor ─────────────────────────────────────────────────────

def _laser_read_int32(addr: int) -> Optional[int]:
    """Signed INT32 from the laser's input registers, high word first
    (the opposite word order to the motor drivers' _read_int32)."""
    if not _client:
        return None
    for attempt in range(RETRIES):
        try:
            with _lock:
                r = _client.read_input_registers(addr, count=2, device_id=LASER["id"])
                if not r.isError() and len(r.registers) == 2:
                    u = (r.registers[0] << 16) | r.registers[1]
                    return u - 0x1_0000_0000 if u >= 0x8000_0000 else u
        except Exception:
            pass
        if attempt < RETRIES - 1:
            time.sleep(0.05)
    return None


def _laser_reading() -> Optional[dict]:
    """One laser reading with the teeth offset applied, or None if it didn't answer."""
    raw = _laser_read_int32(LASER["reg_distance"])
    if raw is None:
        return None
    lo, hi = LASER["range_mm"]
    mm = round(raw * LASER["scale_mm"], 2)
    in_range = lo <= mm <= hi
    offset = _calibration.get("laser_teeth_offset_mm")
    below_teeth = round(mm - offset, 2) if offset is not None and in_range else None
    return {"distance_mm": mm, "raw": raw, "in_range": in_range, "range_mm": [lo, hi],
            "teeth_offset_mm": offset, "below_teeth_mm": below_teeth}


@app.get("/laser/read")
def laser_read():
    if not _connected:
        return _err("not connected")
    reading = _laser_reading()
    if reading is None:
        return _err(f"no reply from the laser (ID {LASER['id']})")
    return _ok(**reading)


class LaserOffsetReq(BaseModel):
    offset_mm: float


@app.post("/calibration/save_laser_offset")
def save_laser_offset(req: LaserOffsetReq):
    global _calibration
    _calibration["laser_teeth_offset_mm"] = round(req.offset_mm, 2)
    _save_calibration(_calibration)
    _log(f"  [calibration] laser to TubeGrip teeth offset saved — {req.offset_mm:.2f} mm")
    return _ok(offset_mm=_calibration["laser_teeth_offset_mm"])


class LaserScanZReq(BaseModel):
    z_mm: float


@app.post("/calibration/save_laser_scan_z")
def save_laser_scan_z(req: LaserScanZReq):
    global _calibration
    lim = MOTORS["z_gripper"]["lim"]
    if not 0 <= req.z_mm <= lim:
        return _err(f"scan height must be between 0 and {lim} mm (z_gripper limit)")
    _calibration["laser_scan_z_mm"] = round(req.z_mm, 2)
    _save_calibration(_calibration)
    _log(f"  [calibration] laser scan height saved — z_gripper {req.z_mm:.2f} mm")
    return _ok(z_mm=_calibration["laser_scan_z_mm"])


class LaserStandReq(BaseModel):
    x: float
    y: float


def _laser_gripper_offset() -> Optional[dict]:
    """How far the gripper is from the laser spot, as {dx, dy} in mm: an
    object the laser sees at machine x/y is under the gripper at
    x + dx, y + dy. Derived from the pipetting stand — the gripper's
    calibrated stand position minus the laser's stand position. None until
    both are saved."""
    stand = _calibration.get("pipetting_stand")
    laser = _calibration.get("laser_stand_xy")
    if not stand or not laser:
        return None
    return {"dx": round(stand["x"] - laser["x"], 2), "dy": round(stand["y"] - laser["y"], 2)}


@app.post("/calibration/save_laser_stand_xy")
def save_laser_stand_xy(req: LaserStandReq):
    global _calibration
    for axis, v in (("x", req.x), ("y", req.y)):
        lim = MOTORS[axis]["lim"]
        if not 0 <= v <= lim:
            return _err(f"{axis} must be between 0 and {lim} mm")
    _calibration["laser_stand_xy"] = {"x": round(req.x, 2), "y": round(req.y, 2)}
    _save_calibration(_calibration)
    off = _laser_gripper_offset()
    _log(f"  [calibration] laser pipetting stand position saved — x={req.x:.2f} y={req.y:.2f}"
         + (f" (gripper offset dx={off['dx']:+.2f} dy={off['dy']:+.2f} mm)" if off else
            " (calibrate the pipetting stand to get the gripper offset)"))
    return _ok(laser_stand_xy=_calibration["laser_stand_xy"], gripper_offset=off)


# Laser scan: home all, lower z_gripper to the scan height and hold it there,
# then sweep x 0 → max, step y, sweep x max → 0, ... until y max, sampling the
# laser the whole time. Only in-range readings count as "detected"; runs of
# consecutive detections in one sweep are merged into one row of the results.
LASER_SCAN_Y_STEP_MM = 10.0  # x sweeps run at x's normal speed (MOTORS["x"]["rpm"])

_laser_scan: dict = {"running": False, "segments": [], "error": None}


def _laser_seg_set_x(seg: dict, lx_from: float, lx_to: float, xy_off: Optional[dict]) -> None:
    """Sets a detection's laser x edges (machine x where the laser saw each
    edge) and the reported x_from/x_to/y/length — gripper coordinates with
    xy_off (see _laser_gripper_offset), laser coordinates without."""
    dx, dy = (xy_off["dx"], xy_off["dy"]) if xy_off else (0.0, 0.0)
    seg["laser_x_from"], seg["laser_x_to"] = round(lx_from, 2), round(lx_to, 2)
    seg["x_from"], seg["x_to"] = round(lx_from + dx, 2), round(lx_to + dx, 2)
    seg["y"] = round(seg["laser_y"] + dy, 1)
    seg["length_mm"] = round(lx_to - lx_from, 2)
    if xy_off is not None:
        seg["reachable"] = (0 <= seg["x_from"] and seg["x_to"] <= MOTORS["x"]["lim"]
                            and 0 <= seg["y"] <= MOTORS["y"]["lim"])


def _laser_sample() -> Optional[tuple[float, dict]]:
    """One laser reading with the x it was taken at (the mean of an x read
    just before and just after it), or None if a read failed."""
    xcfg = MOTORS["x"]
    x1, _ = _read_encoder_mm(xcfg["ids"], xcfg)
    reading = _laser_reading()
    x2, _ = _read_encoder_mm(xcfg["ids"], xcfg)
    if x1 is None or x2 is None or reading is None:
        return None
    _laser_scan["samples"] += 1
    return (x1 + x2) / 2, reading


def _laser_scan_sweep(target_x: float, y: float, offset: Optional[float],
                     xy_off: Optional[dict]) -> tuple[bool, list[dict]]:
    """Moves x to target_x while sampling the laser. Returns (move ok, the
    detected segments in this sweep). Each edge is put halfway between the
    last reading on one side of it and the first on the other, with
    edge_err_mm = half that gap; _laser_refine_segment narrows it down
    afterwards. An edge with no reading beyond it (the object runs past the
    end of travel) is flagged in open_edges."""
    x_start, _ = _read_encoder_mm(MOTORS["x"]["ids"], MOTORS["x"])
    inc = x_start is None or target_x >= x_start    # sweeping toward higher x
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("ok", extend_to("x", target_x)), daemon=True)
    t.start()
    segments: list[dict] = []
    seg: Optional[dict] = None
    last_out_x: Optional[float] = None

    def close_segment(x_out: Optional[float]):
        nonlocal seg
        if seg:
            d = seg.pop("_d")
            first, last = seg.pop("_first"), seg.pop("_last")
            edge_a, gap_a = seg.pop("_edge_a"), seg.pop("_gap_a")
            if x_out is None:   # still detecting when the sweep ended
                edge_b, gap_b = last, 0.0
                seg["open_edges"].append("high" if inc else "low")
            else:
                edge_b, gap_b = (last + x_out) / 2, abs(x_out - last)
            lo, hi = sorted((edge_a, edge_b))
            seg["edge_err_mm"] = round(max(gap_a, gap_b) / 2, 2)
            seg["laser_min"], seg["laser_max"] = min(d), max(d)
            if offset is not None:
                seg["teeth_min"] = round(min(d) - offset, 2)
                seg["teeth_max"] = round(max(d) - offset, 2)
            _laser_seg_set_x(seg, lo, hi, xy_off)
            segments.append(seg)
        seg = None

    while True:
        moving = t.is_alive()   # one last sample after the move ends
        sample = _laser_sample()
        if sample is not None:
            x, reading = sample
            if reading["in_range"]:
                if seg is None:
                    seg = {"laser_y": round(y, 1), "n": 0, "_d": [], "_first": x, "refined": False,
                           "open_edges": []}
                    if last_out_x is None:   # already detecting when the sweep started
                        seg["_edge_a"], seg["_gap_a"] = x, 0.0
                        seg["open_edges"].append("low" if inc else "high")
                    else:
                        seg["_edge_a"], seg["_gap_a"] = (last_out_x + x) / 2, abs(x - last_out_x)
                seg["_last"] = x
                seg["n"] += 1
                seg["_d"].append(reading["distance_mm"])
            else:
                close_segment(x)
                last_out_x = x
        if not moving:
            break
    close_segment(None)
    t.join()
    return bool(result.get("ok")), segments


# Edge refinement: after a sweep, each edge of each detection is swept again
# slowly, both ways, over a short window around where the fast sweep put it.
# Readings come every ~30-40 ms whatever the speed, so slow = closely spaced;
# averaging the two directions cancels most of the laser's response lag.
LASER_REFINE_WINDOW_MM = 10.0   # each edge is re-swept from its fast-sweep position -W to +W
LASER_REFINE_RPM = 8            # x speed for the slow passes (x mpr 40 → ~5.3 mm/s)


def _laser_sample_pass(target_x: float) -> tuple[bool, list[tuple[float, bool]]]:
    """Moves x to target_x (at whatever x speed is set) while sampling the
    laser. Returns (move ok, [(x, in range), ...] in the order taken)."""
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("ok", extend_to("x", target_x)), daemon=True)
    t.start()
    samples = []
    while True:
        moving = t.is_alive()
        sample = _laser_sample()
        if sample is not None:
            samples.append((sample[0], sample[1]["in_range"]))
        if not moving:
            break
    t.join()
    return bool(result.get("ok")), samples


def _laser_edge_transitions(samples: list[tuple[float, bool]], side: str) -> list[tuple[float, float]]:
    """(edge x, gap between the two readings) for every in/out change in
    samples. side "low" keeps edges with the object on their high-x side
    (an object's low-x edge), "high" the opposite."""
    edges = []
    for (xa, ina), (xb, inb) in zip(samples, samples[1:]):
        if ina == inb or xa == xb:
            continue
        in_x, out_x = (xb, xa) if inb else (xa, xb)
        if (in_x > out_x) == (side == "low"):
            edges.append(((xa + xb) / 2, abs(xb - xa)))
    return edges


def _laser_refine_edge(edge_x: float, side: str) -> tuple[bool, Optional[tuple[float, float]]]:
    """Slow passes across [edge_x - W, edge_x + W], one each way. Returns
    (moves ok, (edge x, ± mm) or None if neither pass saw the edge)."""
    xcfg = MOTORS["x"]
    x_now, _ = _read_encoder_mm(xcfg["ids"], xcfg)
    a = max(0.0, edge_x - LASER_REFINE_WINDOW_MM)
    b = min(xcfg["lim"], edge_x + LASER_REFINE_WINDOW_MM)
    if x_now is not None and abs(x_now - b) < abs(x_now - a):
        a, b = b, a     # start from the nearer end of the window
    if not extend_to("x", a):
        return False, None
    found = []
    normal_rpm = xcfg["rpm"]
    xcfg["rpm"] = LASER_REFINE_RPM
    try:
        for target in (b, a):
            ok, samples = _laser_sample_pass(target)
            if not ok:
                return False, None
            edges = _laser_edge_transitions(samples, side)
            if edges:
                found.append(min(edges, key=lambda e: abs(e[0] - edge_x)))
    finally:
        xcfg["rpm"] = normal_rpm
    if not found:
        return True, None
    x = sum(e[0] for e in found) / len(found)
    err = max(max(e[1] for e in found) / 2, abs(found[0][0] - found[-1][0]) / 2)
    return True, (x, err)


def _laser_refine_segment(seg: dict, xy_off: Optional[dict]) -> bool:
    """Refines both x edges of one detection in place (the edge nearer the
    current x first). Edges the fast sweep didn't see (open_edges) are left
    as they are. Returns False if a move failed or the scan was stopped."""
    xcfg = MOTORS["x"]
    x_now, _ = _read_encoder_mm(xcfg["ids"], xcfg)
    edges = {"low": seg["laser_x_from"], "high": seg["laser_x_to"]}
    order = sorted(edges, key=lambda k: abs(edges[k] - (x_now if x_now is not None else 0.0)))
    errs = []
    refined_any = False
    for side in order:
        if side in seg["open_edges"]:
            continue
        if _stop_flag.is_set():
            return False
        ok, res = _laser_refine_edge(edges[side], side)
        if not ok:
            return False
        if res is None:
            _log(f"  [laser scan] y={seg['laser_y']} {side} edge near x={edges[side]:.1f} not seen on the slow "
                 f"passes — keeping the fast-sweep value")
            continue
        edges[side], err = res
        errs.append(err)
        refined_any = True
    if refined_any:
        lo, hi = sorted((edges["low"], edges["high"]))
        _laser_seg_set_x(seg, lo, hi, xy_off)
        closed = [e for e in ("low", "high") if e not in seg["open_edges"]]
        if len(errs) == len(closed):     # every edge that can be refined was
            seg["edge_err_mm"] = round(max(errs), 2)
            seg["refined"] = True
        _log(f"  [laser scan] y={seg['laser_y']} refined: laser x {lo:.2f} – {hi:.2f} mm, "
             f"length {hi - lo:.2f} mm (±{seg['edge_err_mm']} mm per edge)")
    return True


def _laser_scan_task(scan_z: float) -> None:
    global _laser_scan
    offset = _calibration.get("laser_teeth_offset_mm")
    xy_off = _laser_gripper_offset()
    x_max, y_max = MOTORS["x"]["lim"], MOTORS["y"]["lim"]
    rows = [i * LASER_SCAN_Y_STEP_MM for i in range(int(y_max // LASER_SCAN_Y_STEP_MM) + 1)]
    if rows[-1] < y_max:
        rows.append(y_max)
    _laser_scan.update(running=True, phase="homing", row=0, rows=len(rows), samples=0,
                       segments=[], error=None, offset_mm=offset, gripper_offset=xy_off, scan_z_mm=scan_z,
                       x_max=x_max, y_max=y_max, started=time.strftime("%H:%M:%S"), finished=None)

    def fail(msg: str) -> None:
        _laser_scan["error"] = msg
        _log(f"  [laser scan] {msg}")

    try:
        _log(f"  [laser scan] starting — x 0 → {x_max} mm, y 0 → {y_max} mm in "
             f"{LASER_SCAN_Y_STEP_MM} mm steps ({len(rows)} sweeps), z_gripper at {scan_z} mm; "
             + (f"results in gripper x/y (laser offset dx={xy_off['dx']:+.2f} dy={xy_off['dy']:+.2f} mm)"
                if xy_off else "results in laser x/y (laser pipetting stand position not set)"))
        if not _home_all_sequence():
            return fail("stopped" if _stop_flag.is_set() else "home all failed — scan aborted")
        _laser_scan["phase"] = "lowering z"
        if not extend_to("z_gripper", scan_z):
            return fail("could not move z_gripper to the scan height — scan aborted")
        _laser_scan["phase"] = "scanning"
        for i, y in enumerate(rows):
            if _stop_flag.is_set():
                return fail("stopped")
            _laser_scan["row"] = i + 1
            if i > 0 and not extend_to("y", y):
                return fail("stopped" if _stop_flag.is_set() else f"y move to {y} mm failed — scan aborted")
            target_x = x_max if i % 2 == 0 else 0.0
            ok, segs = _laser_scan_sweep(target_x, y, offset, xy_off)
            _laser_scan["segments"].extend(segs)
            if not ok:
                return fail("stopped" if _stop_flag.is_set() else f"x sweep at y={y} mm failed — scan aborted")
            if segs:
                for k, g in enumerate(segs):
                    _laser_scan["phase"] = f"refining edges — sweep {i + 1}, detection {k + 1} of {len(segs)}"
                    if not _laser_refine_segment(g, xy_off):
                        return fail("stopped" if _stop_flag.is_set() else f"edge refinement at y={y} mm failed — scan aborted")
                # back to the end of this sweep, so the next one covers the whole row
                if not extend_to("x", target_x):
                    return fail("stopped" if _stop_flag.is_set() else "x move failed — scan aborted")
                _laser_scan["phase"] = "scanning"
        _log(f"  [laser scan] complete — {len(_laser_scan['segments'])} detection(s) "
             f"from {_laser_scan['samples']} readings")
        _laser_scan["phase"] = "homing after scan"
        _log("  [laser scan] homing all motors")
        if not _home_all_sequence():
            fail("stopped during the home after the scan" if _stop_flag.is_set()
                 else "scan finished but home all afterwards failed")
    finally:
        _laser_scan.update(running=False, phase="done", finished=time.strftime("%H:%M:%S"))


@app.post("/laser/scan")
def laser_scan():
    if not _connected:
        return _err("not connected")
    if _is_busy() or _manual_mode:
        return _err("busy")
    scan_z = _calibration.get("laser_scan_z_mm")
    if scan_z is None:
        return _err("set and save the scan height first")
    if _laser_read_int32(LASER["reg_distance"]) is None:
        return _err(f"no reply from the laser (ID {LASER['id']})")
    _log_op("laser scan")
    if not _run_bg(_laser_scan_task, scan_z):
        return _err(_bg_refusal())
    return _ok(started=True)


@app.get("/laser/scan/state")
def laser_scan_state():
    return _ok(**_laser_scan)


@app.get("/laser/info")
def laser_info():
    if not _connected:
        return _err("not connected")
    baud = _laser_read_int32(LASER["reg_baud"])
    if baud is None:
        return _err(f"no reply from the laser (ID {LASER['id']})")
    return _ok(id=LASER["id"], baud=baud, bus_baud=_bus_baud, is_laser=baud == _bus_baud,
               range_mm=list(LASER["range_mm"]), scale_mm=LASER["scale_mm"])


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    cursor = len(_log_lines)
    try:
        while True:
            await asyncio.sleep(0.4)
            lines = list(_log_lines)
            new_lines = lines[cursor:]
            cursor = len(lines)
            pos = {}
            laser = {"live": False}
            if _connected:
                try:
                    pos = await asyncio.get_running_loop().run_in_executor(None, read_positions, True)
                except Exception:
                    pass
                if _laser_live:
                    try:
                        reading = await asyncio.get_running_loop().run_in_executor(None, _laser_reading)
                    except Exception:
                        reading = None
                    laser = {"live": True, "reading": reading}
                # Live position readout for Spin — lets you disable holding
                # torque and rotate it by hand while watching the encoder
                # value update, to sanity-check the mechanism.
                try:
                    GRIPPER_SPIN["position"] = await asyncio.get_running_loop().run_in_executor(
                        None, _read_int32, GRIPPER_SPIN["id"], 0x0004, True)
                except Exception:
                    pass
                # Real holding-torque state from the driver (0x00D4: 0 =
                # enabled, 1 = released), so the release button follows the
                # hardware — any Spin move (including the close before every
                # x/y move) silently re-enables it.
                try:
                    regs = await asyncio.get_running_loop().run_in_executor(
                        None, _read_once, GRIPPER_SPIN["id"], GRIPPER_REG_ENABLE, 1)
                    GRIPPER_SPIN["enabled"] = None if regs is None else (regs[0] & 1) == 0
                except Exception:
                    pass
                # Same live readout for TubeGrip — also lets you watch the
                # moveto/measure/home sequence progress in real time.
                try:
                    GRIPPER_TUBEGRIP["position"] = await asyncio.get_running_loop().run_in_executor(
                        None, _read_int32, GRIPPER_TUBEGRIP["id"], 0x0004, True)
                except Exception:
                    pass
            try:
                await websocket.send_json({
                    "user":         _current_user,
                    "machine":      _current_machine,
                    "connected":    _connected,
                    "busy":         _is_busy(),
                    "homed":        dict(_is_homed),
                    "manual_mode":  _manual_mode,
                    "showcase_phase": _showcase_phase,
                    "showcase_loop": _showcase_loop_progress,
                    "showcase_loop_last": _showcase_loop_last,
                    "positions":    pos,
                    "log":          new_lines,
                    "tubegrip":     GRIPPER_TUBEGRIP,
                    "spin":         GRIPPER_SPIN,
                    "holder_plates": _calibration.get("holder_plates", {}),
                    "laser":        laser,
                })
            except Exception:
                break
    except WebSocketDisconnect:
        pass


# ── Pipettor protocol (SOCON/SOPA ADP SC-STxxx, RS-485 ASCII framing) ──────────
# Ported from PiP_A/pipettor_control.py. This is SOCON's own proprietary
# ASCII framing over RS-485 -- NOT Modbus -- so it gets its own transport,
# device driver, and connection state, entirely separate from the
# ModbusSerialClient/_client the x/y/z motors and grippers use above. See
# that file's module docstring for the full protocol writeup (frame format,
# wiring, worked example from the manual).

PIPETTOR_HEAD_OEM = 0x5B    # '['
PIPETTOR_HEAD_DEBUG = 0x2F  # '/'
PIPETTOR_TAIL = 0x45        # 'E'

# Section 5.10.2 "Standard error" code table, verbatim from the manual.
PIPETTOR_ERROR_CODES = {
    0x00: "No error",
    0x01: "Last action not completed",
    0x02: "Device is not initialized",
    0x03: "Equipment overload",
    0x04: "Invalid instruction",
    0x05: "Level detection fault",
    0x06: "Reservation 1",
    0x07: "Timeout",
    0x08: "Failed to execute the instruction",
    0x09: "Instruction buffer overflow",
    0x0A: "Begin executing the command",
    0x0B: "Tip missing",
    0x0C: "Not registered",
    0x0D: "Empty detected while aspiration",
    0x0E: "Block detected while aspiration",
    0x0F: "Reservation 2",
    0x10: "Foam detected while aspiration",
    0x11: "Suction exceeds the capacity of TIP",
    0x12: "Pressure out of range",
    0x13: "Insufficient liquid detected while aspiration",
    0x14: "Empty detected while dispensing",
    0x15: "Block detected while dispensing",
}

# Non-fatal status codes -- 0x00 = idle/success, 0x0A = command has started
# executing (this is normal for a movement command's first ack frame).
PIPETTOR_NON_ERROR_CODES = {0x00, 0x0A}

# Section 5.6 <Q[n]> query sub-codes -- informational labels only.
PIPETTOR_QUERY_CODES = {
    1: "Acceleration (increments/s)",
    2: "Start speed (increments/s)",
    3: "Cutoff / break speed (increments/s)",
    6: "Max speed (increments/s)",
    8: "Node address + MAC (bytes 2-7)",
    11: "Port (baud) rate",
    12: "Auxiliary address + MAC (bytes 2-7)",
    18: "Current position (increments)",
    21: "Starting current",
    22: "Operating current",
    23: "Holding current",
    25: "Foam / bubble low limit",
    28: "Tip presence (0=no tip, 1=tip present)",
    29: "Calibration coefficient",
    30: "Air suction (empty) threshold",
    31: "Block threshold",
    32: "Foam threshold",
    33: "Compensation deviation",
    34: "Liquid level detection speed",
    40: "Empty threshold",
    41: "Discharge/exclusion threshold",
}


class PipettorProtocolError(Exception):
    """Raised when a response frame is malformed (bad length/tail/checksum)."""


class PipettorTimeoutError(Exception):
    """Raised when no response was received from the device in time."""


def _pipettor_fmt_num(value) -> str:
    """Format a number the way the manual's examples do: no decimal point
    for whole numbers, otherwise up to 3 decimal places with no trailing
    zeros (e.g. 200 -> "200", 1.5 -> "1.5", 1.040 -> "1.04")."""
    v = float(value)
    if v == int(v):
        return str(int(v))
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s


def _pipettor_clamp(value, lo, hi):
    v = float(value)
    return max(lo, min(hi, v))


def _pipettor_encode_request(address: int, command_str: str, head: int = PIPETTOR_HEAD_OEM) -> bytes:
    body = bytes([head]) + str(int(address)).encode("ascii") + command_str.encode("ascii") + bytes([PIPETTOR_TAIL])
    checksum = sum(body) & 0xFF
    return body + bytes([checksum])


def _pipettor_encode_response(address: int, error_code: int, payload7: bytes = b"", head: int = PIPETTOR_HEAD_DEBUG) -> bytes:
    """Build a well-formed 13-byte response frame. Used by the simulated
    transport to build canned replies."""
    payload7 = (payload7 or b"")[:7].ljust(7, b"\x00")
    data = bytes([0x06, error_code & 0xFF]) + payload7
    body = bytes([head, address & 0xFF]) + data + bytes([PIPETTOR_TAIL])
    checksum = sum(body) & 0xFF
    return body + bytes([checksum])


def _pipettor_decode_response(raw: bytes) -> dict:
    if len(raw) != 13:
        raise PipettorProtocolError(f"Expected a 13-byte response, got {len(raw)} byte(s): {raw.hex()}")
    head, address = raw[0], raw[1]
    data = raw[2:11]
    tail, checksum = raw[11], raw[12]
    calc = sum(raw[:12]) & 0xFF
    if tail != PIPETTOR_TAIL:
        raise PipettorProtocolError(f"Bad tail byte 0x{tail:02X} (expected 0x45 'E'). Raw: {raw.hex()}")
    if checksum != calc:
        raise PipettorProtocolError(f"Checksum mismatch: frame has 0x{checksum:02X}, computed 0x{calc:02X}. Raw: {raw.hex()}")
    return {
        "head": head,
        "address": address,
        "status_byte0": data[0],
        "error_code": data[1],
        "data_rest": bytes(data[2:]),
        "raw": raw,
    }


def _pipettor_leading_number(s: str, default=0.0) -> float:
    out = ""
    for ch in s:
        if ch.isdigit() or ch == "." or (ch == "-" and not out):
            out += ch
        else:
            break
    try:
        return float(out) if out not in ("", "-", ".") else default
    except ValueError:
        return default


class PipettorSerialTransport:
    """Real RS-485 link via a USB-RS485 adapter, through pyserial. Only one
    of these exists at a time (see _pipettor_device below), so unlike the
    x/y/z motor bus's ModbusSerialClient this doesn't need its own
    reconnect/retry plumbing -- connect/disconnect just replace it."""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0):
        if not HAVE_PYSERIAL:
            raise RuntimeError("pyserial is not installed in this environment.")
        self.lock = threading.Lock()
        self.ser = serial.Serial(
            port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=timeout, write_timeout=timeout,
        )

    def write(self, data: bytes):
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self.ser.write(data)

    def read(self, n: int, timeout: Optional[float] = None) -> bytes:
        if timeout is not None:
            old = self.ser.timeout
            self.ser.timeout = timeout
            try:
                return self.ser.read(n)
            finally:
                self.ser.timeout = old
        return self.ser.read(n)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    @property
    def is_open(self):
        try:
            return bool(self.ser.is_open)
        except Exception:
            return False


class PipettorSimulatedTransport:
    """In-memory stand-in for the pipettor, so the tab can be exercised
    without hardware attached. Mimics the two-frame ack/completion pattern
    for action commands, and returns plausible canned data for others."""

    def __init__(self, address: int = 1):
        self.address = address
        self.position_ul = 0.0
        self.initialized = False
        self.lock = threading.Lock()
        self._queue: collections.deque = collections.deque()

    def write(self, data: bytes):
        try:
            i = 1
            while i < len(data) and 48 <= data[i] <= 57:
                i += 1
            addr_str = data[1:i].decode("ascii", errors="ignore") or str(self.address)
            cmd = data[i:-2].decode("ascii", errors="ignore")
        except Exception:
            addr_str, cmd = str(self.address), ""
        addr = int(addr_str) if addr_str.isdigit() else self.address
        self._queue.clear()
        for frame in self._build_replies(addr, cmd):
            self._queue.append(frame)

    def read(self, n: int, timeout: Optional[float] = None) -> bytes:
        if not self._queue:
            time.sleep(min(timeout or 0.05, 0.05))
            return b""
        time.sleep(0.05)
        return self._queue.popleft()

    def _build_replies(self, addr, cmd):
        letter = cmd[:1]
        if letter in ("H", "R", "A", "P", "D", "L", "F"):
            if letter == "H":
                self.initialized = True
            if letter == "P":
                self.position_ul = _pipettor_clamp(self.position_ul + _pipettor_leading_number(cmd[1:], 0), 0, 1100)
            elif letter == "D":
                self.position_ul = _pipettor_clamp(self.position_ul - _pipettor_leading_number(cmd[1:], 0), 0, 1100)
            elif letter == "A":
                self.position_ul = _pipettor_clamp(_pipettor_leading_number(cmd[1:], 0), 0, 1000)
            code = 0x00 if self.initialized or letter == "H" else 0x02
            return [
                _pipettor_encode_response(addr, 0x0A, b"\x30" * 7),
                _pipettor_encode_response(addr, code, b"\x30" * 7),
            ]
        if letter == "V":
            return [_pipettor_encode_response(addr, 0x00, b"0104E")]  # fake firmware id
        if letter == "Q":
            sub = _pipettor_leading_number(cmd[1:], 0)
            fake = {6: "3000", 1: "300000", 2: "200", 3: "200", 18: str(int(self.position_ul))}
            payload = fake.get(int(sub), "0").encode("ascii")
            return [_pipettor_encode_response(addr, 0x00, payload)]
        # generic setting commands (speed, thresholds, calibration, etc.)
        return [_pipettor_encode_response(addr, 0x00, b"\x30" * 7)]

    def close(self):
        pass

    @property
    def is_open(self):
        return True


@dataclass
class PipettorCommandResult:
    ok: bool
    action: str
    command_sent: str
    address: int
    error_code: int
    error_name: str
    busy: bool
    data_hex: str
    data_ascii: str
    raw_sent_hex: str
    raw_recv_hex: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self):
        return asdict(self)


class PipettorDevice:
    # Default head is OEM ('['), matching the manual's Appendix D worked
    # example -- see pipettor_control.py's PipettorDevice docstring for the
    # full reasoning.
    def __init__(self, transport, address: int = 1, head: int = PIPETTOR_HEAD_OEM):
        self.transport = transport
        self.address = address
        self.head = head

    def send(self, command_str: str, ack_timeout: float = 1.5, motion_timeout: float = 20.0) -> PipettorCommandResult:
        letter = command_str[:1]
        frame = _pipettor_encode_request(self.address, command_str, head=self.head)

        with self.transport.lock:
            self.transport.write(frame)
            raw = self.transport.read(13, timeout=ack_timeout)
            final_raw = raw
            # On real hardware every command comes back as an immediate 0x0A
            # "begin executing" ack followed by a second frame with the real
            # result, even for plain queries like V/Q.
            if raw:
                try:
                    first = _pipettor_decode_response(raw)
                    if first["error_code"] == 0x0A:
                        second = self.transport.read(13, timeout=motion_timeout)
                        if second:
                            final_raw = second
                except PipettorProtocolError:
                    pass

        _PIPETTOR_COMM_LOG.append({
            "ts": time.time(),
            "address": self.address,
            "command": command_str,
            "sent_hex": frame.hex(),
            "recv_hex": final_raw.hex() if final_raw else "",
        })

        if not final_raw:
            raise PipettorTimeoutError(
                f"No response from device at address {self.address} for command {command_str!r}. "
                "Check wiring (RS485+/-), serial port, baud rate, and that the address matches the "
                "pipettor's toggle-switch/Softcontrol setting."
            )

        parsed = _pipettor_decode_response(final_raw)
        # Action commands (H/R/A/P/D/L/F) use the manual's fixed
        # [0x06][error_code][7 bytes] completion layout. Query-style replies
        # (V, Q[n]) don't -- the whole 9-byte data field is the answer.
        if parsed["status_byte0"] == 0x06:
            code = parsed["error_code"]
            payload = parsed["data_rest"]
        else:
            code = 0x00
            payload = bytes([parsed["status_byte0"], parsed["error_code"]]) + parsed["data_rest"]
        return PipettorCommandResult(
            ok=code in PIPETTOR_NON_ERROR_CODES,
            action=letter,
            command_sent=command_str,
            address=parsed["address"],
            error_code=code,
            error_name=PIPETTOR_ERROR_CODES.get(code, f"Unknown code 0x{code:02X}"),
            busy=(code == 0x0A),
            data_hex=payload.hex(),
            data_ascii="".join(chr(b) if 32 <= b < 127 else "." for b in payload),
            raw_sent_hex=frame.hex(),
            raw_recv_hex=final_raw.hex(),
        )

    # --- movement -----------------------------------------------------
    def initialize(self):
        return self.send("H")

    def eject_tip(self):
        return self.send("R")

    def move_absolute(self, volume_ul):
        return self.send(f"A{_pipettor_fmt_num(_pipettor_clamp(volume_ul, 0, 1000))}")

    def aspirate(self, volume_ul):
        return self.send(f"P{_pipettor_fmt_num(_pipettor_clamp(volume_ul, 0, 1100))}")

    def dispense(self, volume_ul):
        return self.send(f"D{_pipettor_fmt_num(_pipettor_clamp(volume_ul, 0, 1100))}")

    def aspirate_full(self, volume_ul, max_speed=None, start_speed=None, acceleration=None, check_liquid=False):
        if acceleration is not None:
            self.set_acceleration(acceleration)
        if start_speed is not None:
            self.set_start_speed(start_speed)
        if max_speed is not None:
            self.set_max_speed(max_speed)
        if check_liquid:
            self.enable_detection(True)
        result = self.aspirate(volume_ul)
        if check_liquid:
            self.enable_detection(False)
        return result

    def dispense_full(self, volume_ul, max_speed=None, start_speed=None, acceleration=None):
        if acceleration is not None:
            self.set_acceleration(acceleration)
        if start_speed is not None:
            self.set_start_speed(start_speed)
        if max_speed is not None:
            self.set_max_speed(max_speed)
        return self.dispense(volume_ul)

    def stop(self):
        return self.send("S")

    def delay(self, ms):
        return self.send(f"M{_pipettor_fmt_num(_pipettor_clamp(ms, 0, 30000))}")

    # --- speed / motion -------------------------------------------------
    def set_max_speed(self, v):
        return self.send(f"s{_pipettor_fmt_num(_pipettor_clamp(v, 500, 10000))}")

    def set_start_speed(self, v):
        return self.send(f"b{_pipettor_fmt_num(_pipettor_clamp(v, 100, 10000))}")

    def set_cutoff_speed(self, v):
        return self.send(f"c{_pipettor_fmt_num(_pipettor_clamp(v, 100, 10000))}")

    def set_acceleration(self, v):
        return self.send(f"a{_pipettor_fmt_num(_pipettor_clamp(v, 30000, 1000000))}")

    def set_retraction_viscosity(self, v):
        return self.send(f"]{_pipettor_fmt_num(_pipettor_clamp(v, 1, 100))}")

    # --- suction/discharge detection thresholds -------------------------
    def enable_detection(self, on):
        return self.send(f"f{1 if on else 0}")

    def set_empty_threshold(self, v):
        return self.send(f"${_pipettor_fmt_num(_pipettor_clamp(v, 0, 800))}")

    def set_low_air_limit(self, v):
        return self.send(f"i{_pipettor_fmt_num(_pipettor_clamp(v, 0, 800))}")

    def set_foam_threshold(self, v):
        return self.send(f"!{_pipettor_fmt_num(_pipettor_clamp(v, 0, 800))}")

    def set_foam_floor(self, v):
        return self.send(f"J{_pipettor_fmt_num(_pipettor_clamp(v, 0, 800))}")

    def set_block_threshold(self, v):
        return self.send(f"%{_pipettor_fmt_num(_pipettor_clamp(v, 0, 800))}")

    def set_discharge_threshold(self, v):
        return self.send(f"{{{_pipettor_fmt_num(_pipettor_clamp(v, 0, 800))}")

    def set_discharge_limit(self, v):
        return self.send(f"}}{_pipettor_fmt_num(_pipettor_clamp(v, 0, 800))}")

    # --- calibration / tip -----------------------------------------------
    def set_calibration_coefficient(self, v):
        return self.send(f"j{_pipettor_fmt_num(_pipettor_clamp(v, 1.0, 2.0))}")

    def set_compensation_deviation(self, v):
        return self.send(f"e{_pipettor_fmt_num(_pipettor_clamp(v, -100, 100))}")

    def set_tip_capacity(self, v):
        return self.send(f"C{_pipettor_fmt_num(_pipettor_clamp(v, 10, 1100))}")

    def write_config(self):
        return self.send("W")

    # --- liquid level detection -------------------------------------------
    def start_level_detection(self, sensitivity=5):
        return self.send(f"L{_pipettor_fmt_num(_pipettor_clamp(sensitivity, 3, 40))}")

    def set_lld_method(self, capacitive):
        return self.send(f"m{1 if capacitive else 0}")

    def set_lld_speed(self, v):
        return self.send(f"k{_pipettor_fmt_num(_pipettor_clamp(v, 100, 2000))}")

    def set_lld_mode(self, mode):
        return self.send(f"G{int(_pipettor_clamp(mode, 0, 3))}")

    def set_lld_timeout(self, ms):
        return self.send(f"#{_pipettor_fmt_num(_pipettor_clamp(ms, 0, 50000))}")

    # --- diagnostics / reporting -------------------------------------------
    def run_diagnostics(self):
        return self.send("F1")

    def report_firmware_version(self):
        return self.send("V")

    def query(self, code):
        return self.send(f"Q{int(code)}")

    def check_tip_mounted(self):
        return self.query(28)


# ── Pipettor state + settings ───────────────────────────────────────────────────

_pipettor_device: Optional[PipettorDevice] = None
_pipettor_lock = threading.Lock()   # guards connect/disconnect swapping _pipettor_device
_pipettor_info = {"connected": False, "port": None, "baud": None, "address": None, "simulate": False}
_PIPETTOR_COMM_LOG: collections.deque = collections.deque(maxlen=300)

PIPETTOR_DEFAULT_SETTINGS = {
    "port": "", "baud": 115200, "address": 1, "head": "oem",
    "max_speed": 3000, "start_speed": 200, "cutoff_speed": 200, "acceleration": 300000,
    "retraction_viscosity": 30,
    "detection_enabled": False, "empty_threshold": 0, "low_air_limit": 0,
    "foam_threshold": 0, "foam_floor": 0, "block_threshold": 350,
    "discharge_threshold": 0, "discharge_limit": 0,
    "calibration_coefficient": 1.0, "compensation_deviation": 0.0, "tip_capacity": 1000,
    "lld_sensitivity": 5, "lld_method_capacitive": False, "lld_speed": 100,
    "lld_mode": 0, "lld_timeout": 10000,
}


def _pipettor_load_settings() -> dict:
    settings = dict(PIPETTOR_DEFAULT_SETTINGS)
    if PIPETTOR_CONFIG_FILE.exists():
        try:
            settings.update(json.loads(PIPETTOR_CONFIG_FILE.read_text()))
        except Exception as e:
            _log(f"[pipettor] could not read saved config ({e}); using defaults.")
    return settings


def _pipettor_save_settings(settings: dict) -> None:
    PIPETTOR_CONFIG_FILE.write_text(json.dumps(settings, indent=2))


_PIPETTOR_SETTINGS = _pipettor_load_settings()

_PIPETTOR_ALLOWED_ACTIONS = {
    "initialize", "eject_tip", "move_absolute", "aspirate", "dispense", "aspirate_full", "dispense_full",
    "stop", "delay", "check_tip_mounted",
    "set_max_speed", "set_start_speed", "set_cutoff_speed", "set_acceleration", "set_retraction_viscosity",
    "enable_detection", "set_empty_threshold", "set_low_air_limit", "set_foam_threshold", "set_foam_floor",
    "set_block_threshold", "set_discharge_threshold", "set_discharge_limit",
    "set_calibration_coefficient", "set_compensation_deviation", "set_tip_capacity", "write_config",
    "start_level_detection", "set_lld_method", "set_lld_speed", "set_lld_mode", "set_lld_timeout",
    "run_diagnostics", "report_firmware_version", "query",
}

# Q[n] sub-codes behind the "refresh ADP" button (empty/foam/block thresholds
# + calibration ratio/offset) -- see PIPETTOR_QUERY_CODES for what each is.
_PIPETTOR_REFRESH_ADP_QUERIES = {
    "empty_threshold": 30,
    "foam_threshold": 32,
    "block_threshold": 31,
    "cal_ratio": 29,
    "cal_offset": 33,
}


def _pipettor_cleanup_on_exit():
    if _pipettor_device is not None:
        try:
            _pipettor_device.transport.close()
        except Exception:
            pass


atexit.register(_pipettor_cleanup_on_exit)


# ── Pipettor endpoints ────────────────────────────────────────────────────────

class PipettorConnectReq(BaseModel):
    port: str = ""
    baud: int = 115200
    address: int = 1
    head: str = "oem"
    simulate: bool = False


class PipettorCommandReq(BaseModel):
    action: str
    params: dict = {}


@app.get("/pipettor/ports")
def pipettor_ports():
    if not HAVE_PYSERIAL:
        return {"ports": [], "pyserial_available": False}
    ports = [{"device": p.device, "description": p.description} for p in serial.tools.list_ports.comports()]
    return {"ports": ports, "pyserial_available": True}


def _probe_pipettor(port: str, baud: int, address: int, head: int) -> bool:
    """Sends one read-only query (Q18, current position) and accepts the
    port only if a well-formed reply frame (13 bytes, 'E' tail, valid
    checksum) comes back."""
    try:
        t = PipettorSerialTransport(port=port, baud=baud, timeout=0.4)
    except Exception:
        return False
    try:
        t.write(_pipettor_encode_request(address, "Q18", head=head))
        raw = t.read(13, timeout=0.5)
        if len(raw) != 13:
            return False
        try:
            _pipettor_decode_response(raw)
        except PipettorProtocolError:
            return False
        t.read(13, timeout=0.3)   # swallow the second frame that follows a 0x0A ack
        return True
    except Exception:
        return False
    finally:
        t.close()

def _find_pipettor(baud: int, address: int, head: int, quiet: bool = False) -> Optional[str]:
    """First USB serial port, other than the motor bus's and the static
    gripper's, where the pipettor at `address` answers."""
    skip = {_bus_port, _gripper_port} - {None}
    tried = []
    for port in _serial_candidates():
        if port in skip:
            continue
        tried.append(port)
        if _probe_pipettor(port, baud, address, head):
            _log(f"[pipettor] found pipettor (address {address}) on {port}")
            return port
    if not quiet:
        _log(f"[pipettor] pipettor (address {address}) not found — tried {tried or 'no free USB serial ports'}")
    return None


_pipettor_dev = None   # _dev_id of the pipettor's port when it was opened


@app.post("/pipettor/connect")
def pipettor_connect(req: PipettorConnectReq):
    global _pipettor_device, _pipettor_info, _pipettor_dev, _pipettor_lost
    head = PIPETTOR_HEAD_OEM if req.head == "oem" else PIPETTOR_HEAD_DEBUG
    auto = not req.simulate and _is_auto(req.port)
    _pipettor_lost = None

    with _pipettor_lock:
        if _pipettor_device is not None:
            try:
                _pipettor_device.transport.close()
            except Exception:
                pass
            _pipettor_device = None

        port = req.port
        if auto:
            port = _find_pipettor(req.baud, req.address, head)
            if port is None:
                _pipettor_info = {"connected": False, "port": None, "baud": req.baud, "address": req.address,
                                   "simulate": False, "head": req.head, "auto": True}
                return _err(f"pipettor (address {req.address}) not found on any free USB serial port — "
                            "is it plugged in and powered?")
        try:
            if req.simulate:
                transport = PipettorSimulatedTransport(address=req.address)
            else:
                transport = PipettorSerialTransport(port=port, baud=req.baud)
            _pipettor_device = PipettorDevice(transport, address=req.address, head=head)
            _pipettor_dev = None if req.simulate else _dev_id(port)
            _pipettor_info = {"connected": True, "port": port, "baud": req.baud,
                               "address": req.address, "simulate": req.simulate, "head": req.head,
                               "auto": auto}
        except Exception as e:
            _pipettor_info = {"connected": False, "port": req.port, "baud": req.baud,
                               "address": req.address, "simulate": req.simulate, "head": req.head}
            return _err(str(e))

    _PIPETTOR_SETTINGS["port"] = req.port
    _PIPETTOR_SETTINGS["baud"] = req.baud
    _PIPETTOR_SETTINGS["address"] = req.address
    _PIPETTOR_SETTINGS["head"] = req.head
    _pipettor_save_settings(_PIPETTOR_SETTINGS)
    _log(f"[pipettor] connected — {'simulated' if req.simulate else port} @ {req.baud}, address {req.address}")
    return _ok(info=_pipettor_info)


@app.post("/pipettor/disconnect")
def pipettor_disconnect():
    global _pipettor_device, _pipettor_info, _pipettor_lost
    _pipettor_lost = None
    with _pipettor_lock:
        if _pipettor_device is not None:
            try:
                _pipettor_device.transport.close()
            except Exception:
                pass
        _pipettor_device = None
        _pipettor_info = {"connected": False, "port": None, "baud": None, "address": None, "simulate": False}
    _log("[pipettor] disconnected")
    return _ok()


@app.get("/pipettor/status")
def pipettor_status():
    if _pipettor_device is None:
        return {"connected": False}
    try:
        result = _pipettor_device.query(6)  # harmless heartbeat query (max speed report)
        return {"connected": True, "info": _pipettor_info, "result": result.to_dict()}
    except Exception as e:
        return {"connected": True, "info": _pipettor_info, "error": str(e)}


@app.post("/pipettor/command")
def pipettor_command(req: PipettorCommandReq):
    if req.action not in _PIPETTOR_ALLOWED_ACTIONS:
        return _err(f"Unknown action {req.action!r}")
    if _pipettor_device is None:
        return _err("Not connected to a device yet. Use the Connection panel first.")
    try:
        method = getattr(_pipettor_device, req.action)
        result = method(**req.params)
        return _ok(result=result.to_dict())
    except (RuntimeError, PipettorTimeoutError, PipettorProtocolError) as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}", code=500)


@app.get("/pipettor/refresh_adp")
def pipettor_refresh_adp():
    if _pipettor_device is None:
        return _err("Not connected to a device yet. Use the Connection panel first.")
    values = {}
    for key, code in _PIPETTOR_REFRESH_ADP_QUERIES.items():
        try:
            r = _pipettor_device.query(code)
            values[key] = r.data_ascii.rstrip(".")
        except Exception:
            values[key] = None
    return _ok(values=values)


@app.get("/pipettor/log")
def pipettor_log(n: int = 30):
    return {"log": list(_PIPETTOR_COMM_LOG)[-n:]}


@app.post("/pipettor/save_config")
def pipettor_save_config(payload: dict):
    _PIPETTOR_SETTINGS.update(payload)
    _pipettor_save_settings(_PIPETTOR_SETTINGS)
    return _ok()


@app.get("/pipettor/settings")
def pipettor_get_settings():
    return _PIPETTOR_SETTINGS


@app.get("/pipettor/error_codes")
def pipettor_error_codes():
    return {str(k): v for k, v in PIPETTOR_ERROR_CODES.items()}


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>OpenShelf Cherry Picker</title>
<link rel="icon" type="image/png" href="/assets/brand/favicon.png"/>
<style>
/* Branding from opshelf.com, same as zeon_cherrypicker.py: Outfit, yellow
   actions with navy text, 8px corners, on a black page. Fonts and logo are
   local (assets/brand) so the page looks right with no internet. */
@font-face{font-family:Outfit;font-weight:400;font-display:swap;src:url(/assets/brand/Outfit-Regular.ttf) format("truetype");}
@font-face{font-family:Outfit;font-weight:500;font-display:swap;src:url(/assets/brand/Outfit-Medium.ttf) format("truetype");}
@font-face{font-family:Outfit;font-weight:600;font-display:swap;src:url(/assets/brand/Outfit-SemiBold.ttf) format("truetype");}
@font-face{font-family:Outfit;font-weight:700;font-display:swap;src:url(/assets/brand/Outfit-Bold.ttf) format("truetype");}
:root{color-scheme:dark;--yellow:#ffc900;--background:#000;--blue:#6f8bff;--red:#ff2424;--green:#35d07f;
  --navy:#070c29;--text:#f2f2f2;--muted:#a8a8a8;--line:#3a3a3a;--panel:#161616;--panel-head:#242424;
  /* Lighter than the brand red/orange so small status text stays readable on black. */
  --red-text:#ff7070;--warn-text:#ffb547;--radius:8px;}
*{box-sizing:border-box;margin:0;padding:0;font-family:Outfit,Arial,sans-serif;font-size:14px;}
body{background:var(--background);color:var(--text);padding:20px;font-variant-numeric:tabular-nums;}
h1{font-size:18px;font-weight:500;color:var(--text);}
h3{font-size:13px;font-weight:600;margin:0 0 6px;color:var(--muted);}
h3::first-letter,thead td::first-letter{text-transform:uppercase;}
a{color:var(--blue);}
table{border-collapse:separate;border-spacing:0;border:1px solid var(--line);border-radius:var(--radius);
  overflow:hidden;background:var(--panel);margin-bottom:14px;width:100%;}
td,th{border-bottom:1px solid var(--line);padding:7px 12px;text-align:left;vertical-align:middle;}
tr:last-child>td{border-bottom:none;}
thead td{font-weight:600;font-size:13px;color:var(--text);background:var(--panel-head);}
button{padding:8px 16px;border:1px solid var(--line);border-radius:var(--radius);background:var(--panel);
  color:var(--text);cursor:pointer;font-weight:500;white-space:nowrap;transition:opacity .15s;}
button::first-letter{text-transform:uppercase;}
button:hover{opacity:.8;}
button:active{opacity:.7;transform:translateY(1px);}
button:disabled{opacity:.35;cursor:default;transform:none;}
button.primary{background:var(--yellow);border-color:var(--yellow);color:var(--navy);}
button.danger{background:var(--red);border-color:var(--red);color:#fff;font-weight:600;}
button:focus-visible,select:focus,input:focus,textarea:focus{outline:2px solid var(--blue);outline-offset:1px;}
select,input[type=text],input[type=number],textarea{border:1px solid var(--line);border-radius:6px;padding:6px 10px;
  background:var(--panel);color:var(--text);}
input[type=checkbox]{accent-color:var(--yellow);}
input[type=text]{width:160px;}
input[type=number]{width:90px;}
.btn-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;}
.ok{color:var(--green);font-weight:600;}
.err{color:var(--red-text);font-weight:600;}
.warn{color:var(--warn-text);font-weight:600;}
.muted{color:var(--muted);}
.section{margin-bottom:14px;}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
@media(max-width:640px){.grid2{grid-template-columns:1fr;}}
/* Same treatment as the site's navbar: page colour, logo left; a line instead
   of its soft shadow, which would not show on black. */
.header{display:flex;justify-content:space-between;align-items:center;gap:12px;
  margin:-20px -20px 18px;padding:12px 20px;background:var(--background);border-bottom:1px solid var(--line);}
.brand{display:flex;align-items:center;gap:12px;}
.brand img{height:30px;width:auto;display:block;}
#log{padding:10px;background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
     height:240px;overflow-y:auto;font-size:12px;line-height:1.5;}
#log *{font-size:12px;}
#sessionTable{width:100%;}
#sessionTable td,#sessionTable th{border-bottom:1px solid var(--line);padding:4px 8px;font-size:12px;}
#sessionTable thead td{font-weight:600;background:var(--panel-head);}
.modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;
  background:rgba(0,0,0,.7);z-index:1000;justify-content:center;align-items:center;}
.modal-overlay.active{display:flex;}
.modal-box{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:24px;max-width:720px;
  width:92%;max-height:82vh;overflow-y:auto;position:relative;font-size:13px;}
.modal-box h2{font-size:16px;font-weight:600;margin:16px 0 6px;color:var(--text);}
.modal-box h3{font-size:14px;font-weight:600;margin:14px 0 4px;color:var(--muted);}
.modal-box pre{background:var(--background);border:1px solid var(--line);border-radius:6px;padding:8px;overflow-x:auto;margin:6px 0;}
.modal-box pre,.modal-box code{font-family:monospace;font-size:12px;}
.modal-box code{background:var(--panel-head);padding:1px 4px;border-radius:4px;}
.modal-box hr{border:none;border-top:1px solid var(--line);margin:12px 0;}
.modal-box table{width:100%;margin:8px 0;}
.modal-box table td,.modal-box table th{border-bottom:1px solid var(--line);padding:4px 8px;}
.modal-box table thead td{background:var(--panel-head);font-weight:600;}
.modal-close{position:sticky;top:0;float:right;margin:-4px -4px 8px 8px;
  padding:4px 10px;border:1px solid var(--line);border-radius:var(--radius);background:var(--panel-head);
  color:var(--text);cursor:pointer;font-size:12px;}
.tab-bar{display:flex;gap:4px;margin-bottom:16px;border-bottom:1px solid var(--line);flex-wrap:wrap;}
.tab-btn{padding:9px 16px;border:none;border-bottom:3px solid transparent;border-radius:0;background:none;
  color:var(--muted);margin-bottom:-1px;}
.tab-btn.active{color:var(--text);border-bottom-color:var(--yellow);font-weight:600;}
.well-plate{display:inline-grid;gap:5px;margin-top:8px;}
.well-hdr{text-align:center;font-size:11px;font-weight:600;color:var(--muted);line-height:1;}
.well-row-lbl{font-size:11px;font-weight:600;color:var(--muted);display:flex;align-items:center;
  justify-content:flex-end;padding-right:4px;}
.well{width:34px;height:34px;border-radius:50%;border:1.5px solid #5a5a5a;background:var(--panel);
  cursor:pointer;padding:0;}
.well:hover{background:var(--panel-head);border-color:var(--muted);}
.well.has-tube{background:var(--blue);border-color:#4f68d6;}
.well.has-tube:hover{background:#5c78f0;border-color:#3f58c6;}
.well.in-range{box-shadow:0 0 0 2.5px var(--yellow);}
.well.in-range.has-tube{box-shadow:0 0 0 2.5px var(--yellow);}
.well.selected{background:var(--yellow);border-color:var(--yellow);}
.edit-tube-active .well{cursor:crosshair;}
.edit-tube-active .well:hover{background:#ffe066;border-color:#ccaa00;}
.edit-tube-active .well.has-tube:hover{background:#e07040;border-color:#c05020;}
#wellInfo{margin-top:12px;font-size:13px;}
</style>
</head>
<body>
<div class="header">
  <div class="brand"><img src="/assets/brand/openshelf-logo-yellow.svg" alt="openshelf"/><h1>Cherry Picker</h1></div>
</div>

<div class="tab-bar">
  <button class="tab-btn active" id="tab_control" onclick="showTab('control')">control</button>
  <button class="tab-btn" id="tab_tube" onclick="showTab('tube')" style="display:none">tube selector</button>
  <button class="tab-btn" id="tab_holder" onclick="showTab('holder')" style="display:none">tube holder</button>
  <button class="tab-btn" id="tab_stand" onclick="showTab('stand')" style="display:none">pipetting stand</button>
  <button class="tab-btn" id="tab_capholder" onclick="showTab('capholder')" style="display:none">cap holder</button>
  <button class="tab-btn" id="tab_pipettor" onclick="showTab('pipettor')" style="display:none">pipettor</button>
  <button class="tab-btn" id="tab_laser" onclick="showTab('laser')" style="display:none">laser</button>
</div>

<div id="panel_control">

<!-- Login -->
<div class="section" id="loginSection">
<table>
<thead><tr><td colspan="2">user login</td></tr></thead>
<tbody>
<tr><td>name</td><td><input type="text" id="userName" placeholder="enter your name"/></td></tr>
<tr><td>machine ID</td><td><input type="text" id="machineId" placeholder="e.g. Unit-01"/></td></tr>
<tr><td colspan="2">
  <div class="btn-row">
    <button class="primary" onclick="doLogin()">log in</button>
    <button onclick="showReadme()">? readme</button>
    <span id="loginStatus" class="warn"></span>
  </div>
</td></tr>
</tbody>
</table>
</div>

<!-- Recent Logins (public) -->
<div class="section">
<table>
<thead><tr><td colspan="5">recent logins</td></tr>
<tr>
  <td style="font-weight:bold">user</td>
  <td style="font-weight:bold">machine</td>
  <td style="font-weight:bold">login</td>
  <td style="font-weight:bold">logout</td>
  <td style="font-weight:bold">duration (min)</td>
</tr></thead>
<tbody id="recentBody"><tr><td colspan="5" style="color:var(--muted)">loading...</td></tr></tbody>
</table>
</div>

<!-- Error History (public) -->
<div class="section">
<table id="errorTable">
<thead><tr>
  <td colspan="6"><span>error history</span> &nbsp;
    <button style="font-size:11px;padding:2px 8px" onclick="loadErrors()">refresh</button>
  </td>
</tr>
<tr>
  <td style="font-weight:bold">date</td>
  <td style="font-weight:bold">user</td>
  <td style="font-weight:bold">machine</td>
  <td style="font-weight:bold">time</td>
  <td style="font-weight:bold">motor</td>
  <td style="font-weight:bold">description</td>
</tr></thead>
<tbody id="errorBody"><tr><td colspan="6" style="color:var(--muted)">loading...</td></tr></tbody>
</table>
</div>

<!-- Connection (hidden until logged in) -->
<div class="section" id="connSection" style="display:none">
<table>
<thead><tr><td colspan="2"><span>bus connection</span> — <span id="currentUser" style="font-weight:bold"></span> &nbsp;|&nbsp; <span>machine</span>: <span id="currentMachine" style="font-weight:bold"></span></td></tr></thead>
<tbody>
<tr><td>port</td><td><input type="text" id="port" value="auto"/>
  <span style="font-size:11px;color:var(--muted)">auto = find the motor bus and static gripper automatically, or type a port like /dev/ttyUSB0</span></td></tr>
<tr><td>baud</td><td>
  <select id="baud">
    <option>9600</option><option>19200</option><option>57600</option>
    <option selected>115200</option>
  </select>
</td></tr>
<tr><td colspan="2">
  <div class="btn-row">
    <button id="btnConn" class="primary" onclick="doConnect()">connect</button>
    <button id="btnDisc" onclick="doDisconnect()" disabled>disconnect</button>
    <button onclick="doLogout()" style="margin-left:12px">log out</button>
    <button onclick="showReadme()">? readme</button>
    <span id="connStatus" class="warn">disconnected</span>
  </div>
</td></tr>
</tbody>
</table>
</div>

<!-- Encoder positions -->
<div class="section" id="statusSection" style="display:none">
<table>
<thead><tr><td colspan="4">encoder positions</td></tr></thead>
<tbody>
<tr>
  <td style="font-weight:bold">motor</td>
  <td style="font-weight:bold">position</td>
  <td style="font-weight:bold">unit</td>
  <td style="font-weight:bold">homed</td>
</tr>
<tr>
  <td>y</td>
  <td id="pos_y">—</td>
  <td>mm</td>
  <td id="homed_y">—</td>
</tr>
<tr>
  <td>x</td>
  <td id="pos_x">—</td>
  <td>mm</td>
  <td id="homed_x">—</td>
</tr>
<tr>
  <td>z gripper</td>
  <td id="pos_z_gripper">—</td>
  <td>mm</td>
  <td id="homed_z_gripper">—</td>
</tr>
<tr>
  <td>z pipettor</td>
  <td id="pos_z_pipettor">—</td>
  <td>mm</td>
  <td id="homed_z_pipettor">—</td>
</tr>
</tbody>
</table>
</div>

<!-- Controls -->
<div class="section" id="ctrlSection" style="display:none">
<div class="grid2">

  <!-- Home -->
  <table>
  <thead><tr><td colspan="2">home</td></tr></thead>
  <tbody>
  <tr><td>motor</td><td>
    <select id="homeMotor">
      <option value="all">all (y + x + z gripper + z pipettor)</option>
      <option value="y">y</option>
      <option value="x">x</option>
      <option value="z_gripper">z gripper</option>
      <option value="z_pipettor">z pipettor</option>
    </select>
  </td></tr>
  <tr><td colspan="2">
    <button class="primary" id="btnHome" onclick="doHome()">home</button>
  </td></tr>
  <tr><td>showcase decap</td><td>
    <button class="primary" id="btnShowcaseBegin" onclick="doShowcaseStep('begin')"
      title="place the tube in the tube holder by hand first — closes the static gripper, decaps, and parks the cap at z=95 mm, x=5 mm">begin</button>
    <button class="primary" id="btnShowcaseFinish" onclick="doShowcaseStep('finish')" style="display:none"
      title="recaps the tube and parks z at 95 mm, x at 5 mm">finished</button>
    <button class="primary" id="btnShowcaseRelease" onclick="doShowcaseStep('release')" style="display:none"
      title="opens the static gripper so the tube can be taken out">release tube</button>
    <span id="showcaseHint" style="font-size:11px;color:var(--muted)">place the tube in the tube holder, then press begin</span>
  </td></tr>
  <tr><td>loop decap</td><td>
    <input type="number" id="showcaseLoopCycles" placeholder="∞" min="1" step="1" style="width:60px"
      title="number of cycles — leave blank to loop until stop"/>
    <button class="primary" id="btnShowcaseLoop" onclick="doShowcaseLoop()"
      title="runs begin → finished → release tube back to back, with the tube left in the tube holder">loop decap</button>
    <button class="danger loop-stop" onclick="doStop()" disabled>stop</button>
    <span id="showcaseLoopCount" style="font-weight:bold"></span>
    <span style="font-size:11px;color:var(--muted)">(leave blank for ∞)</span>
  </td></tr>
  </tbody>
  </table>

  <!-- Extend -->
  <table>
  <thead><tr><td colspan="2">extend</td></tr></thead>
  <tbody>
  <tr><td>x mm</td><td>
    <input type="number" id="extX" placeholder="max" step="0.1" min="0"/>
  </td></tr>
  <tr><td>y mm</td><td>
    <input type="number" id="extY" placeholder="max" step="0.1" min="0"/>
  </td></tr>
  <tr><td colspan="2" style="font-size:11px;color:var(--muted)">both z axes home first, then x+y move together — use the z gripper / z pipettor panel for z moves</td></tr>
  <tr><td colspan="2">
    <button class="primary" id="btnExtend" onclick="doExtend()">extend</button>
  </td></tr>
  </tbody>
  </table>

  <!-- Loop -->
  <table>
  <thead><tr><td colspan="2">loop</td></tr></thead>
  <tbody>
  <tr><td>motors</td><td>
    <label style="margin-right:10px"><input type="checkbox" id="loopY" checked/> y</label>
    <label><input type="checkbox" id="loopX" checked/> x</label>
  </td></tr>
  <tr><td>cycles</td><td>
    <input type="number" id="loopN" value="5" min="1" step="1"/>
  </td></tr>
  <tr><td colspan="2">
    <button class="primary" id="btnLoop" onclick="doLoop()">start loop</button>
    <button class="danger loop-stop" onclick="doStop()" disabled>stop</button>
  </td></tr>
  </tbody>
  </table>

  <!-- Limits -->
  <table>
  <thead><tr><td colspan="3">limits</td></tr></thead>
  <tbody>
  <tr>
    <td>y</td>
    <td><input type="number" id="limY" value="170.0" step="0.1" min="0"/></td>
    <td>mm</td>
  </tr>
  <tr>
    <td>x</td>
    <td><input type="number" id="limX" value="550.0" step="1" min="0"/></td>
    <td>mm</td>
  </tr>
  <tr>
    <td>z gripper</td>
    <td><input type="number" id="limZGripper" value="120.0" step="1" min="0"/></td>
    <td>mm</td>
  </tr>
  <tr>
    <td>z pipettor</td>
    <td><input type="number" id="limZPipettor" value="120.0" step="1" min="0"/></td>
    <td>mm</td>
  </tr>
  <tr><td colspan="3">
    <button onclick="doSetLimits()">set limits</button>
  </td></tr>
  </tbody>
  </table>

  <!-- z gripper / z pipettor -->
  <table>
  <thead><tr><td colspan="2">z gripper / z pipettor</td></tr></thead>
  <tbody>
  <tr><td colspan="2" style="font-size:11px;color:var(--muted)">
    independent single-axis actuators, not part of the x/y/z gantry — each has its own home + move-to.
  </td></tr>
  <tr>
    <td>z gripper</td>
    <td>
      <div class="btn-row">
        <button onclick="doHomeSingle('z_gripper')">home</button>
        <input type="number" id="extZGripper" placeholder="mm" step="0.1" min="0" style="width:80px"/>
        <button class="primary" onclick="doExtendSingle('z_gripper','extZGripper')">move to</button>
      </div>
    </td>
  </tr>
  <tr>
    <td>z pipettor</td>
    <td>
      <div class="btn-row">
        <button onclick="doHomeSingle('z_pipettor')">home</button>
        <input type="number" id="extZPipettor" placeholder="mm" step="0.1" min="0" style="width:80px"/>
        <button class="primary" onclick="doExtendSingle('z_pipettor','extZPipettor')">move to</button>
      </div>
    </td>
  </tr>
  </tbody>
  </table>

</div>

<div class="btn-row" style="margin-top:4px">
  <button onclick="doReadPositions()">read positions</button>
  <button class="primary" id="btnTestAll" onclick="doTestAll()">test all</button>
  <button class="danger" id="btnStop" onclick="doStop()" disabled>stop</button>
  <button id="btnManualMode" class="danger" onclick="doToggleManualMode()">enter manual mode</button>
  <span id="manualModeStatus" style="display:none;color:var(--red-text);font-weight:bold">
    MANUAL MODE — torque released, axes free to move by hand
  </span>
  <span id="busyStatus"></span>
</div>
</div>

<!-- Gripper -->
<div class="section" id="gripSection" style="display:none">
<table>
<thead><tr><td colspan="2">Static Gripper</td></tr></thead>
<tbody>
<tr><td colspan="2">
  <div class="btn-row">
    <button class="primary" onclick="doGripper('open')">open</button>
    <button onclick="doGripper('close')">close</button>
    <span id="gripStatus" style="margin-left:8px"></span>
  </div>
</td></tr>
<tr>
  <td>position (%)</td>
  <td>
    <div class="btn-row">
      <input type="number" id="gripPct" value="50" min="0" max="100" step="1" style="width:70px"/>
      <button onclick="doGripperMove()">move</button>
    </div>
  </td>
</tr>
</tbody>
</table>

<table>
<thead><tr><td colspan="2">Spinning Gripper — grip (TubeGrip)</td></tr></thead>
<tbody>
<tr><td colspan="2">
  <div class="btn-row">
    <button class="primary" onclick="doTubegrip('open')">open</button>
    <button onclick="doTubegrip('close')">close</button>
    <span id="tubegripStatus" style="margin-left:8px"></span>
  </div>
</td></tr>
<tr>
  <td>home / measure range</td>
  <td>
    <div class="btn-row">
      <button onclick="doTubegripHome()">home</button>
      <span id="tubegripHomeResult" style="margin-left:8px"></span>
    </div>
  </td>
</tr>
<tr>
  <td>position (%)</td>
  <td>
    <div class="btn-row">
      <input type="number" id="tubegripPct" value="50" min="0" max="100" step="1" style="width:70px"/>
      <button onclick="doTubegripMove()">go to</button>
      <button onclick="doTubegripSetOpen()" title="save this percent as what the 'open' button moves to">set as open</button>
      <button onclick="doTubegripSetClose()" title="save this percent as what the 'close' button moves to">set as close</button>
    </div>
    <div id="tubegripTargets" style="margin-top:4px;font-size:12px;color:var(--muted)"></div>
  </td>
</tr>
<tr>
  <td>holding torque</td>
  <td><div class="btn-row"><button id="tubegripEnableBtn" onclick="doTubegripToggleEnable()">release TubeGrip (move by hand)</button></div></td>
</tr>
<tr>
  <td>alarm</td>
  <td><div class="btn-row"><button onclick="doTubegripClearAlarm()">clear alarm</button><span id="tubegripAlarm" style="margin-left:8px"></span></div></td>
</tr>
</tbody>
</table>

<table>
<thead><tr><td colspan="2">Spinning Gripper — rotation (Spin)</td></tr></thead>
<tbody>
<tr><td colspan="2">
  <div class="btn-row">
    <button class="primary" onclick="doSpin('open')">open</button>
    <button onclick="doSpin('close')">close</button>
    <span id="spinStatus" style="margin-left:8px"></span>
  </div>
</td></tr>
<tr>
  <td>open / close / cap holder (pulses, <span title="pulses per full turn">12000/rev</span>)</td>
  <td>
    <div class="btn-row">
      open <input type="number" id="spinOpenPos" value="0" step="1" style="width:80px" title="open position (pulses)"/>
      <button onclick="doSpinSetCurrent('open', 'spinOpenPos')" title="save Spin's current position as open">set current as open</button>
    </div>
    <div class="btn-row">
      close <input type="number" id="spinClosePos" value="0" step="1" style="width:80px" title="close position (pulses)"/>
      <button onclick="doSpinSetCurrent('close', 'spinClosePos')" title="save Spin's current position as close">set current as close</button>
    </div>
    <div class="btn-row">
      cap holder <input type="number" id="spinCapHolderPos" value="0" step="1" style="width:80px" title="cap holder approach position (pulses)"/>
      <button onclick="doSpinSetCurrent('cap_holder', 'spinCapHolderPos')" title="save Spin's current position as cap holder">set current as cap holder</button>
    </div>
    <div class="btn-row"><button onclick="doSpinSetPositions()">save positions</button></div>
  </td>
</tr>
<tr>
  <td>holding torque</td>
  <td><div class="btn-row"><button id="spinEnableBtn" onclick="doSpinToggleEnable()">release Spin (rotate by hand)</button></div></td>
</tr>
<tr>
  <td>position (live)</td>
  <td><span id="spinPosition" class="ok">—</span></td>
</tr>
<tr>
  <td>rotate to angle (&deg;)</td>
  <td>
    <div class="btn-row">
      <input type="number" id="spinAngle" value="0" step="1" style="width:70px"/>
      <button onclick="doSpinMoveAngle()">move</button>
    </div>
  </td>
</tr>
</tbody>
</table>
</div>

<!-- Grab / Place -->
<div class="section" id="grabPlaceSection" style="display:none">
<table>
<thead><tr><td colspan="3">grab / place</td></tr></thead>
<tbody>
<tr>
  <td>grab</td>
  <td>
    <div class="btn-row">
      <span style="white-space:nowrap">z height (mm)</span>
      <input type="number" id="grabZ" value="60" step="0.1" min="0" style="width:80px" oninput="scheduleHeightsSave()"/>
      <button class="primary" onclick="doGrab()">grab</button>
    </div>
  </td>
</tr>
<tr>
  <td>place</td>
  <td>
    <div class="btn-row">
      <span style="white-space:nowrap">z height (mm)</span>
      <input type="number" id="placeZ" value="60" step="0.1" min="0" style="width:80px" oninput="scheduleHeightsSave()"/>
      <button onclick="doPlace()">place</button>
    </div>
  </td>
</tr>
<tr>
  <td>decap height</td>
  <td>
    <div class="btn-row">
      <span style="white-space:nowrap">z height (mm)</span>
      <input type="number" id="decapZ" value="60" step="0.1" min="0" style="width:80px" oninput="scheduleHeightsSave()"/>
      <button onclick="doDecap()">decap</button>
    </div>
    <span style="font-size:11px;color:var(--muted)">used by both this button and the "decap tube" sequence in Tube Selector</span>
  </td>
</tr>
<tr>
  <td>recap height</td>
  <td>
    <div class="btn-row">
      <span style="white-space:nowrap">z height (mm)</span>
      <input type="number" id="recapZ" value="60" step="0.1" min="0" style="width:80px" oninput="scheduleHeightsSave()"/>
      <button onclick="doRecap()">recap</button>
    </div>
  </td>
</tr>
<tr>
  <td>decap/recap position</td>
  <td>
    <span id="decapRecapHolderPos" style="font-size:12px;color:var(--muted)">using tube holder position — set in "tube holder" tab</span>
  </td>
</tr>
</tbody>
</table>
</div>

<!-- Log -->
<div class="section" id="logSection" style="display:none">
<h3>log</h3>
<div id="log"></div>
</div>

<!-- Session History -->
<div class="section" id="sessSection" style="display:none">
<table id="sessionTable">
<thead><tr>
  <td>user</td>
  <td>login</td>
  <td>logout</td>
  <td>duration (min)</td>
  <td>ops</td>
  <td>errors</td>
</tr></thead>
<tbody id="sessBody"></tbody>
</table>
<div class="btn-row" style="margin-top:6px">
  <button onclick="loadSessions()">refresh session history</button>
</div>
</div>

<!-- Post a Note (shown when logged in) -->
<div class="section" id="notePostSection" style="display:none">
<table>
<thead><tr><td colspan="2">add a note</td></tr></thead>
<tbody>
<tr><td colspan="2">
  <textarea id="noteText" rows="3"
    style="width:100%;font-family:monospace;font-size:12px;border:1px solid var(--line);border-radius:6px;padding:6px;resize:vertical"
    placeholder="write a note..."></textarea>
</td></tr>
<tr><td colspan="2">
  <button class="primary" onclick="doPostNote()">post note</button>
</td></tr>
</tbody>
</table>
</div>

<!-- Notes Board (public) -->
<div class="section">
<table>
<thead><tr><td colspan="4"><span>notes</span> &nbsp;
  <button style="font-size:11px;padding:2px 8px" onclick="loadNotes()">refresh</button>
</td></tr>
<tr>
  <td style="font-weight:bold;width:130px">posted by</td>
  <td style="font-weight:bold;width:110px">machine</td>
  <td style="font-weight:bold;width:150px">time</td>
  <td style="font-weight:bold">note</td>
</tr></thead>
<tbody id="notesBody"><tr><td colspan="4" style="color:var(--muted)">loading...</td></tr></tbody>
</table>
</div>

<!-- README Modal -->
<div class="modal-overlay" id="readmeModal" onclick="if(event.target===this)hideReadme()">
<div class="modal-box">
  <button class="modal-close" onclick="hideReadme()">close</button>
  <div id="readmeContent">loading...</div>
</div>
</div>

</div><!-- end panel_control -->

<!-- Tube Selector Panel -->
<div id="panel_tube" style="display:none">

<div class="section">
<table>
<thead>
<tr><td>tube selector</td></tr>
<tr><td>
  <div class="btn-row">
    <span>plate</span>
    <select id="plateSelect" onchange="switchPlate(this.value)" style="min-width:120px"></select>
    <button onclick="toggleAddPlateForm('addPlateForm', true)">+ add plate</button>
    <button onclick="doDeletePlate()" class="danger">delete plate</button>
    <span id="plateGridInfo" style="font-size:11px;color:var(--muted)"></span>
  </div>
  <div id="addPlateForm" style="display:none;margin-top:6px">
    <div class="btn-row">
      <span>name</span>
      <input type="text" id="newPlateName" placeholder="plate name" style="width:120px"/>
      <span>rows (1, 2, 3…)</span>
      <input type="number" id="newPlateRows" value="12" min="1" max="99" step="1" style="width:54px"/>
      <span>columns (A, B, C…)</span>
      <input type="number" id="newPlateCols" value="8" min="1" max="26" step="1" style="width:54px"/>
    </div>
    <div class="btn-row">
      <span>row pitch</span>
      <input type="number" id="newPlateRowPitch" value="9" min="0" step="0.1" placeholder="mm" style="width:64px"/>
      <span>mm</span>
      <span>column pitch</span>
      <input type="number" id="newPlateColPitch" value="9" min="0" step="0.1" placeholder="mm" style="width:64px"/>
      <span>mm</span>
      <button class="primary" onclick="doAddPlate()">add</button>
      <button onclick="toggleAddPlateForm('addPlateForm', false)">cancel</button>
    </div>
    <span style="font-size:11px;color:var(--muted)">pitch = centre-to-centre spacing. With pitch set, calibrating one corner is enough — the other corners are estimated from it; leave both pitches blank to calibrate all 4 corners.</span>
  </div>
</td></tr>
</thead>
<tbody><tr><td>
  <div id="wellGrid"></div>
  <div id="wellInfo" style="margin-top:10px">
    <span>selected</span>: <strong id="wellLabel">—</strong>
    <span id="wellPos" style="margin-left:14px;font-size:11px;color:var(--muted)"></span>
  </div>
  <div class="btn-row" style="margin-top:8px">
    <button id="btnMoveToWell" onclick="doMoveToWell()">move to well</button>
    <button onclick="doTestPosition()">test position</button>
    <button class="primary" onclick="doDecapTube()">decap tube</button>
  </div>
  <div class="btn-row" style="margin-top:6px">
    <button id="btnEditTubes" onclick="toggleEditTubeMode()">edit tube layout</button>
    <button onclick="fillAllTubes()">fill plate</button>
    <button onclick="clearAllTubes()" class="danger">clear plate</button>
    <span id="editTubesHint" style="font-size:11px;color:var(--muted);display:none">click wells to toggle</span>
  </div>

  <table style="margin-top:14px">
  <thead><tr><td colspan="2">move tube</td></tr></thead>
  <tbody>
  <tr><td colspan="2" style="font-size:11px;color:var(--muted);font-weight:normal">
    tip: to move between plates, set "from", switch the plate above, then set "to"
  </td></tr>
  <tr>
    <td>from</td>
    <td>
      <div class="btn-row">
        <strong id="fromWell">—</strong>
        <span id="fromPos" style="font-size:11px;color:var(--muted)"></span>
        <button onclick="setFromWell()">set from</button>
      </div>
    </td>
  </tr>
  <tr>
    <td>to</td>
    <td>
      <div class="btn-row">
        <strong id="toWell">—</strong>
        <span id="toPos" style="font-size:11px;color:var(--muted)"></span>
        <button onclick="setToWell()">set to</button>
      </div>
    </td>
  </tr>
  <tr><td colspan="2">
    <button class="primary" onclick="doMoveTube()">move tube</button>
  </td></tr>
  </tbody>
  </table>

  <table style="margin-top:14px">
  <thead><tr><td colspan="2">tube shuffle loop</td></tr></thead>
  <tbody>
  <tr>
    <td>plates</td>
    <td>
      <div id="loopPlates" class="btn-row" style="flex-wrap:wrap"></div>
      <span style="font-size:11px;color:var(--muted)">tubes are shuffled randomly across all checked plates</span>
    </td>
  </tr>
  <tr>
    <td>columns</td>
    <td>
      <div class="btn-row">
        <select id="rangeColFrom" onchange="updateRangeHighlight()"></select>
        <span>–</span>
        <select id="rangeColTo" onchange="updateRangeHighlight()"></select>
      </div>
    </td>
  </tr>
  <tr>
    <td>rows</td>
    <td>
      <div class="btn-row">
        <input type="number" id="rangeRowFrom" value="1" min="1" max="12" step="1" style="width:54px" oninput="updateRangeHighlight()"/>
        <span>–</span>
        <input type="number" id="rangeRowTo" value="12" min="1" max="12" step="1" style="width:54px" oninput="updateRangeHighlight()"/>
      </div>
    </td>
  </tr>
  <tr>
    <td>iterations</td>
    <td><input type="number" id="loopIter" value="10" min="1" step="1" style="width:80px"/>
        <span style="font-size:11px;color:var(--muted)">(leave blank for ∞)</span>
    </td>
  </tr>
  <tr><td colspan="2">
    <div class="btn-row">
      <button id="btnLoopStart" class="primary" onclick="doStartTubeLoop()">start loop</button>
      <button id="btnLoopStop" onclick="doStopTubeLoop()" style="display:none">stop</button>
    </div>
  </td></tr>
  <tr><td colspan="2">
    <span id="loopStatus" style="font-size:11px;color:var(--muted)"></span>
  </td></tr>
  </tbody>
  </table>
</td></tr></tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2"><span>calibration — corners</span> — <span id="calibPlateLabel" style="font-weight:normal;color:var(--muted)"></span></td></tr></thead>
<tbody>
<tr><td colspan="2">
  <div class="btn-row" id="cornerBtns"></div>
  <span style="font-size:11px;color:var(--muted)">● calibrated · ◐ estimated from pitch · ○ not calibrated</span>
</td></tr>
<tr>
  <td>x mm</td>
  <td><input type="number" id="calibX" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr>
  <td>z mm</td>
  <td><input type="number" id="calibZ" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr>
  <td>y mm</td>
  <td><input type="number" id="calibY" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr><td colspan="2">
  <div class="btn-row">
    <button onclick="doReadIntoCalib()">read current position</button>
    <button onclick="doMoveToCalib()">move to</button>
    <button class="primary" onclick="doSaveCalib()">save corner</button>
    <button class="primary" onclick="doSetCurrentPosition()">set current position</button>
  </div>
</td></tr>
<tr><td colspan="2">
  <span id="calibStatus" style="font-size:11px;color:var(--muted)"></span>
</td></tr>
</tbody>
</table>
</div>

</div><!-- end panel_tube -->

<!-- Tube Holder Panel -->
<div id="panel_holder" style="display:none">

<div class="section">
<table>
<thead><tr><td colspan="2">tube holder — calibration</td></tr></thead>
<tbody>
<tr>
  <td>x mm</td>
  <td><input type="number" id="holderX" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr>
  <td>z mm</td>
  <td><input type="number" id="holderZ" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr>
  <td>y mm</td>
  <td><input type="number" id="holderY" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr><td colspan="2">
  <div class="btn-row">
    <button onclick="doReadIntoHolder()">read current position</button>
    <button onclick="doMoveToHolder()">move to</button>
    <button class="primary" onclick="doSaveHolder()">save</button>
    <button class="primary" onclick="doSetHolderCurrentPosition()">set current position</button>
  </div>
</td></tr>
<tr><td colspan="2">
  <span id="holderStatus" style="font-size:11px;color:var(--muted)"></span>
</td></tr>
</tbody>
</table>
</div>

</div><!-- end panel_holder -->

<!-- Pipetting Stand Panel -->
<div id="panel_stand" style="display:none">

<div class="section">
<table>
<thead><tr><td colspan="2">pipetting stand — calibration</td></tr></thead>
<tbody>
<tr>
  <td>x mm</td>
  <td><input type="number" id="standX" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr>
  <td>z mm</td>
  <td><input type="number" id="standZ" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr>
  <td>y mm</td>
  <td><input type="number" id="standY" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr><td colspan="2">
  <div class="btn-row">
    <button onclick="doReadIntoStand()">read current position</button>
    <button onclick="doMoveToStand()">move to</button>
    <button class="primary" onclick="doSaveStand()">save</button>
    <button class="primary" onclick="doSetStandCurrentPosition()">set current position</button>
  </div>
</td></tr>
<tr><td colspan="2">
  <span id="standStatus" style="font-size:11px;color:var(--muted)"></span>
</td></tr>
</tbody>
</table>
</div>

</div><!-- end panel_stand -->

<!-- Tube Cap Holder Panel -->
<div id="panel_capholder" style="display:none">

<div class="section">
<table>
<thead>
<tr><td>cap holder</td></tr>
<tr><td>
  <div class="btn-row">
    <span>plate</span>
    <select id="capPlateSelect" onchange="switchCapPlate(this.value)" style="min-width:120px"></select>
    <button onclick="toggleAddPlateForm('addCapPlateForm', true)">+ add plate</button>
    <button onclick="doDeleteCapPlate()" class="danger">delete plate</button>
    <span id="capPlateGridInfo" style="font-size:11px;color:var(--muted)"></span>
  </div>
  <div id="addCapPlateForm" style="display:none;margin-top:6px">
    <div class="btn-row">
      <span>name</span>
      <input type="text" id="newCapPlateName" placeholder="plate name" style="width:120px"/>
      <span>rows (1, 2, 3…)</span>
      <input type="number" id="newCapPlateRows" value="8" min="1" max="99" step="1" style="width:54px"/>
      <span>columns (A, B, C…)</span>
      <input type="number" id="newCapPlateCols" value="6" min="1" max="26" step="1" style="width:54px"/>
    </div>
    <div class="btn-row">
      <span>row pitch</span>
      <input type="number" id="newCapPlateRowPitch" min="0" step="0.1" placeholder="mm" style="width:64px"/>
      <span>mm</span>
      <span>column pitch</span>
      <input type="number" id="newCapPlateColPitch" min="0" step="0.1" placeholder="mm" style="width:64px"/>
      <span>mm</span>
      <button class="primary" onclick="doAddCapPlate()">add</button>
      <button onclick="toggleAddPlateForm('addCapPlateForm', false)">cancel</button>
    </div>
    <span style="font-size:11px;color:var(--muted)">pitch = centre-to-centre spacing. With pitch set, calibrating one corner is enough — the other corners are estimated from it; leave both pitches blank to calibrate all 4 corners.</span>
  </div>
</td></tr>
</thead>
<tbody><tr><td>
  <div id="capGrid"></div>
  <div id="capInfo" style="margin-top:10px">
    <span>selected</span>: <strong id="capLabel">—</strong>
    <span id="capPos" style="margin-left:14px;font-size:11px;color:var(--muted)"></span>
  </div>
  <div class="btn-row" style="margin-top:8px">
    <button id="btnMoveToCap" onclick="doMoveToCap()">move to position</button>
  </div>
  <div class="btn-row" style="margin-top:6px">
    <button id="btnEditCaps" onclick="toggleEditCapMode()">edit layout</button>
    <button onclick="fillAllCaps()">fill plate</button>
    <button onclick="clearAllCaps()" class="danger">clear plate</button>
    <span id="editCapsHint" style="font-size:11px;color:var(--muted);display:none">click positions to toggle</span>
  </div>
</td></tr></tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2"><span>calibration — corners</span> — <span id="capCalibPlateLabel" style="font-weight:normal;color:var(--muted)"></span></td></tr></thead>
<tbody>
<tr><td colspan="2">
  <div class="btn-row" id="capCornerBtns"></div>
  <span style="font-size:11px;color:var(--muted)">● calibrated · ◐ estimated from pitch · ○ not calibrated</span>
</td></tr>
<tr>
  <td>x mm</td>
  <td><input type="number" id="capCalibX" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr>
  <td>z mm</td>
  <td><input type="number" id="capCalibZ" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr>
  <td>y mm</td>
  <td><input type="number" id="capCalibY" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr><td colspan="2">
  <div class="btn-row">
    <button onclick="doReadIntoCapCalib()">read current position</button>
    <button onclick="doMoveToCapCalib()">move to</button>
    <button class="primary" onclick="doSaveCapCalib()">save corner</button>
    <button class="primary" onclick="doSetCapCurrentPosition()">set current position</button>
  </div>
</td></tr>
<tr><td colspan="2">
  <span id="capCalibStatus" style="font-size:11px;color:var(--muted)"></span>
</td></tr>
</tbody>
</table>
</div>

</div><!-- end panel_capholder -->

<!-- Pipettor Panel -->
<div id="panel_pipettor" style="display:none">

<div class="section">
<table>
<thead><tr><td colspan="2">pipettor connection (RS-485)</td></tr></thead>
<tbody>
<tr><td>serial port</td><td>
  <div class="btn-row">
    <select id="pipPort" style="min-width:220px"><option value="auto">auto (find it)</option></select>
    <button onclick="pipRefreshPorts()">refresh ports</button>
  </div>
</td></tr>
<tr><td>baud</td><td>
  <select id="pipBaud">
    <option value="115200" selected>115200</option>
    <option value="9600">9600</option>
  </select>
</td></tr>
<tr><td>device address</td><td><input type="number" id="pipAddress" value="2" min="1" max="255" style="width:80px"/></td></tr>
<tr><td>frame head byte</td><td>
  <select id="pipHead">
    <option value="oem" selected>OEM '[' (0x5B)</option>
    <option value="debug">Debug '/' (0x2F)</option>
  </select>
</td></tr>
<tr><td>simulate</td><td><input type="checkbox" id="pipSimulate"/> <span style="font-size:11px;color:var(--muted)">no hardware needed</span></td></tr>
<tr><td colspan="2">
  <div class="btn-row">
    <button id="pipBtnConn" class="primary" onclick="pipConnect()">connect</button>
    <button id="pipBtnDisc" onclick="pipDisconnect()" disabled>disconnect</button>
    <span id="pipConnStatus" class="warn">disconnected</span>
  </div>
</td></tr>
<tr><td colspan="2" style="font-size:11px;color:var(--muted)">wiring: J1 pin 7 = RS485+ (A) / pin 8 = RS485- (B) — this is a separate RS-485 link from the x/y/z motor bus above.</td></tr>
</tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2">live status</td></tr></thead>
<tbody>
<tr><td>firmware</td><td id="pipFwVersion">—</td></tr>
<tr><td>last error code</td><td id="pipLastErrCode">—</td></tr>
<tr><td>last error text</td><td id="pipLastErrText">—</td></tr>
<tr><td>last raw response</td><td id="pipLastRaw" style="font-family:monospace;font-size:11px">—</td></tr>
<tr><td colspan="2">
  <div class="btn-row">
    <button onclick="pipGetFirmware()">report firmware (V)</button>
    <input type="checkbox" id="pipAutoPoll" checked onchange="pipToggleAutoPoll()"/>
    <span style="font-size:12px">auto-refresh status (2s)</span>
  </div>
</td></tr>
</tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2">ADP control</td></tr></thead>
<tbody>
<tr><td colspan="2">
  <div class="btn-row">
    <button class="primary" onclick="pipCmd('initialize')">ADP initialize</button>
    <button onclick="pipCmd('eject_tip')">drop tip</button>
    <button onclick="pipCheckTip()">check diti</button>
    <span id="pipTipStatus" style="font-size:12px;color:var(--muted)"></span>
    <button style="background:var(--red);border-color:var(--red);color:#fff;font-weight:600;margin-left:auto" onclick="pipCmd('stop')">STOP (S)</button>
  </div>
</td></tr>
</tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2">aspirate</td></tr></thead>
<tbody>
<tr><td>volume (uL)</td><td><input type="number" id="pipAspVol" value="100" style="width:100px"/></td></tr>
<tr><td>max speed</td><td><input type="number" id="pipAspMax" value="3000" style="width:100px"/></td></tr>
<tr><td>start speed</td><td><input type="number" id="pipAspStart" value="200" style="width:100px"/></td></tr>
<tr><td>acceleration</td><td><input type="number" id="pipAspAccel" value="300000" style="width:100px"/></td></tr>
<tr><td>check liquid</td><td><input type="checkbox" id="pipAspCheckLiquid"/> <span style="font-size:11px;color:var(--muted)">empty/foam/block detection</span></td></tr>
<tr><td colspan="2"><button class="primary" onclick="pipAspirate()">aspirate</button></td></tr>
</tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2">dispense</td></tr></thead>
<tbody>
<tr><td>volume (uL)</td><td><input type="number" id="pipDispVol" value="100" style="width:100px"/></td></tr>
<tr><td>max speed</td><td><input type="number" id="pipDispMax" value="3000" style="width:100px"/></td></tr>
<tr><td>start speed</td><td><input type="number" id="pipDispStart" value="200" style="width:100px"/></td></tr>
<tr><td>acceleration</td><td><input type="number" id="pipDispAccel" value="300000" style="width:100px"/></td></tr>
<tr><td colspan="2"><button class="primary" onclick="pipDispense()">dispense</button></td></tr>
</tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2">liquid level detection</td></tr></thead>
<tbody>
<tr><td>sensitivity (3-40)</td><td><input type="number" id="pipLldSens" value="5" style="width:100px"/></td></tr>
<tr><td>speed</td><td><input type="number" id="pipLldSpeed" value="100" style="width:100px"/></td></tr>
<tr><td colspan="2"><button onclick="pipStartDetection()">start detection</button></td></tr>
</tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2">thresholds &amp; calibration &nbsp;
  <button style="font-size:11px;padding:2px 8px" onclick="pipRefreshADP()">refresh ADP</button>
</td></tr>
<tbody>
<tr><td>empty threshold</td><td>
  <div class="btn-row"><input type="number" id="pipThEmpty" style="width:80px"/>
    <button onclick="pipCmd('set_empty_threshold', {v: pipNumVal('pipThEmpty')})">set</button></div>
</td></tr>
<tr><td>foam threshold</td><td>
  <div class="btn-row"><input type="number" id="pipThFoam" style="width:80px"/>
    <button onclick="pipCmd('set_foam_threshold', {v: pipNumVal('pipThFoam')})">set</button></div>
</td></tr>
<tr><td>block threshold</td><td>
  <div class="btn-row"><input type="number" id="pipThBlock" style="width:80px"/>
    <button onclick="pipCmd('set_block_threshold', {v: pipNumVal('pipThBlock')})">set</button></div>
</td></tr>
<tr><td>cal ratio</td><td>
  <div class="btn-row"><input type="number" id="pipCalRatio" step="0.001" style="width:80px"/>
    <button onclick="pipCmd('set_calibration_coefficient', {v: pipNumVal('pipCalRatio')})">set</button></div>
</td></tr>
<tr><td>cal offset</td><td>
  <div class="btn-row"><input type="number" id="pipCalOffset" step="0.001" style="width:80px"/>
    <button onclick="pipCmd('set_compensation_deviation', {v: pipNumVal('pipCalOffset')})">set</button></div>
</td></tr>
</tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2">last action result</td></tr></thead>
<tbody>
<tr><td>action</td><td id="pipActAction">—</td></tr>
<tr><td>status</td><td id="pipActState">—</td></tr>
<tr><td>error code</td><td id="pipActCode">—</td></tr>
<tr><td>data (ascii)</td><td id="pipActDataAscii">—</td></tr>
<tr><td>data (hex)</td><td id="pipActDataHex">—</td></tr>
<tr><td colspan="2" style="font-size:11px;color:var(--muted)">updates after aspirate, dispense, or start detection.</td></tr>
</tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2">diagnostics</td></tr></thead>
<tbody>
<tr><td colspan="2">
  <div class="btn-row">
    <button onclick="pipCmd('run_diagnostics')">run diagnostics (F1)</button>
    <span style="font-size:12px">query code</span>
    <input type="number" id="pipQueryCode" value="6" style="width:80px"/>
    <button onclick="pipCmd('query', {code: pipNumVal('pipQueryCode')})">send query (Q)</button>
  </div>
</td></tr>
</tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td>communication log (raw frames)</td></tr></thead>
</table>
<div id="pipCommLog" style="background:#0b1020;color:#c9d1d9;font-family:monospace;font-size:11px;
  padding:8px;height:180px;overflow-y:auto;white-space:pre-wrap;margin-top:6px"></div>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2">camera</td></tr></thead>
<tbody>
<tr><td colspan="2">
  <div class="btn-row">
    <button id="pipCamToggleBtn" onclick="pipToggleCamera()">open camera</button>
    <button id="pipCamCaptureBtn" onclick="pipCaptureSnapshot()" style="display:none">capture</button>
  </div>
</td></tr>
<tr><td colspan="2">
  <video id="pipCamVideo" autoplay playsinline muted style="display:none;width:100%;max-width:420px;border-radius:6px;background:#000"></video>
  <canvas id="pipCamCanvas" style="display:none"></canvas>
  <img id="pipCamSnapshot" style="display:none;max-width:420px;border-radius:6px;border:1px solid var(--line)"/>
</td></tr>
<tr><td colspan="2" style="font-size:11px;color:var(--muted)">uses your browser's camera (e.g. a USB webcam pointed at the tip/deck) directly in this page.</td></tr>
</tbody>
</table>
</div>

</div><!-- end panel_pipettor -->

<!-- Laser Panel -->
<div id="panel_laser" style="display:none">

<div class="section">
<table>
<thead><tr><td colspan="2">laser distance sensor — NA-W9308 (60–140 mm), motor bus ID 2</td></tr></thead>
<tbody>
<tr>
  <td>from teeth bottom</td>
  <td><span id="laserBelowTeeth" style="font-size:28px;font-weight:bold;font-family:monospace">—</span>
      <span id="laserRange" style="font-size:11px;margin-left:8px"></span></td>
</tr>
<tr>
  <td>laser reading</td>
  <td><span id="laserDist" style="font-family:monospace">—</span></td>
</tr>
<tr>
  <td>raw</td>
  <td><span id="laserRaw" style="font-family:monospace">—</span></td>
</tr>
<tr>
  <td>stats</td>
  <td><span id="laserStats" style="font-family:monospace;font-size:12px">—</span></td>
</tr>
<tr>
  <td>live</td>
  <td><span id="laserLive" class="warn">off — starts when home all completes</span></td>
</tr>
<tr><td colspan="2">
  <div class="btn-row">
    <button onclick="doLaserRead()">read</button>
    <button onclick="clearLaserStats()">clear stats</button>
    <button onclick="doLaserCheck()">check laser</button>
  </div>
</td></tr>
<tr><td colspan="2">
  <span id="laserStatus" style="font-size:11px;color:var(--muted)"></span>
</td></tr>
</tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2">laser to TubeGrip teeth offset</td></tr></thead>
<tbody>
<tr>
  <td>offset mm</td>
  <td><input type="number" id="laserOffset" step="0.01" placeholder="—" style="width:100px"/></td>
</tr>
<tr><td colspan="2">
  <div class="btn-row">
    <button onclick="doLaserOffsetFromReading()">use current reading</button>
    <button class="primary" onclick="doSaveLaserOffset()">save offset</button>
  </div>
</td></tr>
<tr><td colspan="2">
  <span style="font-size:11px;color:var(--muted)">The offset is the laser reading when a flat surface touches the bottom of
  the TubeGrip teeth: hold one there and press "use current reading", then save. "from teeth bottom" is then
  reading − offset — how far the surface is below the teeth (negative = above them).</span>
</td></tr>
<tr><td colspan="2">
  <span id="laserOffsetStatus" style="font-size:11px;color:var(--muted)"></span>
</td></tr>
</tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2">scan height</td></tr></thead>
<tbody>
<tr>
  <td>z_gripper mm</td>
  <td><input type="number" id="laserScanZ" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr><td colspan="2">
  <div class="btn-row">
    <button onclick="doLaserScanZFromCurrent()">use current z</button>
    <button onclick="doMoveToLaserScanZ()">move z to scan height</button>
    <button class="primary" onclick="doSaveLaserScanZ()">save scan height</button>
  </div>
</td></tr>
<tr><td colspan="2">
  <span id="laserScanZStatus" style="font-size:11px;color:var(--muted)"></span>
</td></tr>
</tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2">laser position at the pipetting stand</td></tr></thead>
<tbody>
<tr><td colspan="2">
  <span style="font-size:11px;color:var(--muted)">Jog x/y until the laser spot is on the pipetting stand (where the gripper's
  pipetting stand position is), then save. The difference from the gripper's stand position is the laser's x/y offset,
  and scan results are reported as gripper positions.</span>
</td></tr>
<tr>
  <td>x mm</td>
  <td><input type="number" id="laserStandX" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr>
  <td>y mm</td>
  <td><input type="number" id="laserStandY" step="0.1" placeholder="—" style="width:100px"/></td>
</tr>
<tr><td colspan="2">
  <div class="btn-row">
    <button onclick="doLaserStandFromCurrent()">use current x/y</button>
    <button class="primary" onclick="doSaveLaserStand()">save laser stand position</button>
  </div>
</td></tr>
<tr><td colspan="2">
  <span id="laserStandStatus" style="font-size:11px;color:var(--muted)"></span>
</td></tr>
</tbody>
</table>
</div>

<div class="section">
<table>
<thead><tr><td colspan="2">scan</td></tr></thead>
<tbody>
<tr><td colspan="2">
  <span style="font-size:11px;color:var(--muted)">Homes all motors, lowers z_gripper to the scan height and keeps it
  there, then sweeps x from 0 to max, steps y by 10 mm, sweeps back, and so on until y max, then homes all motors again.
  After each sweep that detects something, both x edges of every detection are swept again slowly to measure where the object starts and ends.
  z stays lowered during every x/y move — make sure nothing is in its path.</span>
</td></tr>
<tr><td colspan="2">
  <div class="btn-row">
    <button class="primary" id="btnLaserScan" onclick="doLaserScan()">scan</button>
    <button class="danger" onclick="post('/stop')">stop</button>
  </div>
</td></tr>
<tr><td colspan="2">
  <span id="laserScanStatus" style="font-size:11px;color:var(--muted)"></span>
</td></tr>
<tr><td colspan="2"><div id="laserScanResults"></div></td></tr>
</tbody>
</table>
</div>

</div><!-- end panel_laser -->

<script>
// ── Tab switching ─────────────────────────────────────────────────────────────
function showTab(name) {
  ['control','tube','holder','stand','capholder','pipettor','laser'].forEach(t => {
    document.getElementById('panel_' + t).style.display = t === name ? '' : 'none';
    document.getElementById('tab_' + t).classList.toggle('active', t === name);
  });
  if (name === 'laser') pollLaserScan();
}

// ── Laser distance sensor ─────────────────────────────────────────────────────
let _laserBusy = false;
let _laserSamples = [];
let _laserLast = null;   // last in-range distance, for "use current reading"
let _laserStatsKind = null;   // 'teeth' or 'laser' — what _laserSamples holds

function laserStatus(msg) {
  document.getElementById('laserStatus').textContent = msg;
}

function showLaserStats() {
  const n = _laserSamples.length;
  if (!n) { document.getElementById('laserStats').textContent = '—'; return; }
  const min = Math.min(..._laserSamples), max = Math.max(..._laserSamples);
  const mean = _laserSamples.reduce((a, b) => a + b, 0) / n;
  document.getElementById('laserStats').textContent =
    `${_laserStatsKind === 'teeth' ? 'from teeth' : 'laser reading'}: n=${n}  min ${min.toFixed(2)}  max ${max.toFixed(2)}  ` +
    `mean ${mean.toFixed(2)}  spread ${(max - min).toFixed(2)} mm`;
}

function clearLaserStats() {
  _laserSamples = [];
  showLaserStats();
}

async function doLaserRead() {
  if (_laserBusy) return false;
  _laserBusy = true;
  try {
    const d = await fetch('/laser/read').then(r => r.json()).catch(e => ({ok: false, error: String(e)}));
    return renderLaserReading(d.ok ? d : null, d.error || 'read failed');
  } finally {
    _laserBusy = false;
  }
}

// one reading from /laser/read or the WS stream; null = no reply
function renderLaserReading(d, error) {
  if (!d) {
    document.getElementById('laserDist').textContent = '—';
    document.getElementById('laserRange').textContent = '';
    document.getElementById('laserBelowTeeth').textContent = '—';
    laserStatus(error || 'no reply from the laser');
    return false;
  }
  const range = document.getElementById('laserRange');
  document.getElementById('laserDist').textContent = d.distance_mm.toFixed(2) + ' mm';
  document.getElementById('laserRaw').textContent = d.raw;
  range.textContent = d.in_range ? 'in range' : `out of range (${d.range_mm[0]}–${d.range_mm[1]} mm)`;
  range.className = d.in_range ? 'ok' : 'warn';
  _laserLast = d.in_range ? d.distance_mm : null;
  document.getElementById('laserBelowTeeth').textContent =
    d.teeth_offset_mm === null ? 'set the teeth offset below'
    : d.below_teeth_mm === null ? '—'
    : d.below_teeth_mm.toFixed(2) + ' mm';
  // stats follow the teeth distance once an offset is saved, the raw laser distance until then
  const kind = d.teeth_offset_mm === null ? 'laser' : 'teeth';
  if (kind !== _laserStatsKind) { _laserSamples = []; _laserStatsKind = kind; }
  if (d.in_range) {
    _laserSamples.push(kind === 'teeth' ? d.below_teeth_mm : d.distance_mm);
    showLaserStats();
  }
  laserStatus(`read at ${new Date().toLocaleTimeString()}`);
  return true;
}

// WS payload: {live: false} until home all completes, then {live: true, reading}
function updateLaserLive(laser) {
  const el = document.getElementById('laserLive');
  if (!laser || !laser.live) {
    el.textContent = 'off — starts when home all completes';
    el.className = 'warn';
    return;
  }
  el.textContent = 'on';
  el.className = 'ok';
  renderLaserReading(laser.reading, 'no reply from the laser');
}

function showLaserScanZ(z) {
  const s = document.getElementById('laserScanZStatus');
  if (z === undefined || z === null) { s.textContent = 'not set yet'; return; }
  document.getElementById('laserScanZ').value = z;
  s.textContent = `saved: z_gripper ${Number(z).toFixed(2)} mm`;
}

async function doLaserScanZFromCurrent() {
  const s = document.getElementById('laserScanZStatus');
  const d = await fetch('/positions').then(r => r.json()).catch(e => ({ok: false, error: String(e)}));
  const z = d.ok && d.positions.z_gripper ? d.positions.z_gripper.val : null;
  if (z === null || z === undefined) { s.textContent = d.error || 'could not read z_gripper'; return; }
  document.getElementById('laserScanZ').value = z.toFixed(2);
  s.textContent = `filled from current z ${z.toFixed(2)} mm — press save to store`;
}

async function doMoveToLaserScanZ() {
  const z = parseFloat(document.getElementById('laserScanZ').value);
  const s = document.getElementById('laserScanZStatus');
  if (isNaN(z)) { s.textContent = 'enter a scan height first'; return; }
  const d = await post('/extend', {motor: 'z_gripper', mm: z}).catch(e => ({ok: false, error: String(e)}));
  s.textContent = d.ok ? `moving z_gripper to ${z.toFixed(2)} mm` : (d.error || 'z move failed');
}

async function doSaveLaserScanZ() {
  const z = parseFloat(document.getElementById('laserScanZ').value);
  const s = document.getElementById('laserScanZStatus');
  if (isNaN(z)) { s.textContent = 'enter a scan height first'; return; }
  const d = await post('/calibration/save_laser_scan_z', {z_mm: z}).catch(e => ({ok: false, error: String(e)}));
  if (!d.ok) { s.textContent = d.error || 'save failed'; return; }
  showLaserScanZ(d.z_mm);
}

// laser x/y at the pipetting stand → laser offset from the gripper
function showLaserStand(laser, stand) {
  const s = document.getElementById('laserStandStatus');
  if (!laser) { s.textContent = 'not set yet — scan results will be in laser x/y'; return; }
  document.getElementById('laserStandX').value = laser.x;
  document.getElementById('laserStandY').value = laser.y;
  let txt = `saved: x=${Number(laser.x).toFixed(2)}  y=${Number(laser.y).toFixed(2)}`;
  if (stand) {
    const dx = stand.x - laser.x, dy = stand.y - laser.y;
    txt += ` — gripper offset from laser: dx=${dx >= 0 ? '+' : ''}${dx.toFixed(2)}  dy=${dy >= 0 ? '+' : ''}${dy.toFixed(2)} mm`;
  } else {
    txt += ' — calibrate the pipetting stand to get the gripper offset';
  }
  s.textContent = txt;
}

async function doLaserStandFromCurrent() {
  const s = document.getElementById('laserStandStatus');
  const d = await fetch('/positions').then(r => r.json()).catch(e => ({ok: false, error: String(e)}));
  const x = d.ok && d.positions.x ? d.positions.x.val : null;
  const y = d.ok && d.positions.y ? d.positions.y.val : null;
  if (x === null || x === undefined || y === null || y === undefined) { s.textContent = d.error || 'could not read x/y'; return; }
  document.getElementById('laserStandX').value = x.toFixed(2);
  document.getElementById('laserStandY').value = y.toFixed(2);
  s.textContent = `filled from current x=${x.toFixed(2)} y=${y.toFixed(2)} — press save to store`;
}

async function doSaveLaserStand() {
  const x = parseFloat(document.getElementById('laserStandX').value);
  const y = parseFloat(document.getElementById('laserStandY').value);
  const s = document.getElementById('laserStandStatus');
  if (isNaN(x) || isNaN(y)) { s.textContent = 'enter x and y first'; return; }
  const d = await post('/calibration/save_laser_stand_xy', {x, y}).catch(e => ({ok: false, error: String(e)}));
  if (!d.ok) { s.textContent = d.error || 'save failed'; return; }
  loadCalibration();
}

function showLaserOffset(offset) {
  const s = document.getElementById('laserOffsetStatus');
  if (offset === undefined || offset === null) { s.textContent = 'not set yet'; return; }
  document.getElementById('laserOffset').value = offset;
  s.textContent = `saved: ${Number(offset).toFixed(2)} mm`;
}

async function doLaserOffsetFromReading() {
  const ok = await doLaserRead();
  const s = document.getElementById('laserOffsetStatus');
  if (!ok || _laserLast === null) { s.textContent = 'no in-range reading — is a surface 60–140 mm from the laser?'; return; }
  document.getElementById('laserOffset').value = _laserLast.toFixed(2);
  s.textContent = `filled from reading ${_laserLast.toFixed(2)} mm — press save to store`;
}

async function doSaveLaserOffset() {
  const v = parseFloat(document.getElementById('laserOffset').value);
  const s = document.getElementById('laserOffsetStatus');
  if (isNaN(v)) { s.textContent = 'enter an offset first'; return; }
  const d = await post('/calibration/save_laser_offset', {offset_mm: v}).catch(e => ({ok: false, error: String(e)}));
  if (!d.ok) { s.textContent = d.error || 'save failed'; return; }
  showLaserOffset(d.offset_mm);
}

// ── Laser scan ──
let _laserScanTimer = null;

async function doLaserScan() {
  const z = document.getElementById('laserScanZ').value;
  if (!confirm(`Scan will home all motors, lower z_gripper to ${z || 'the scan height'} mm and ` +
               `sweep x/y with z kept down. Is the work area clear?`)) return;
  const d = await post('/laser/scan').catch(e => ({ok: false, error: String(e)}));
  if (!d.ok) { document.getElementById('laserScanStatus').textContent = d.error || 'scan failed to start'; return; }
  pollLaserScan();
}

function pollLaserScan() {
  clearTimeout(_laserScanTimer);
  refreshLaserScan().then(running => {
    if (running) _laserScanTimer = setTimeout(pollLaserScan, 1000);
  });
}

// shows the current or last scan; returns true while one is running
async function refreshLaserScan() {
  const d = await fetch('/laser/scan/state').then(r => r.json()).catch(() => null);
  if (!d || !d.ok || !d.started) return false;
  const st = document.getElementById('laserScanStatus');
  document.getElementById('btnLaserScan').disabled = d.running;
  if (d.running) {
    st.textContent = d.phase === 'scanning'
      ? `scanning — sweep ${d.row} of ${d.rows}, ${d.segments.length} detection(s) so far`
      : `${d.phase}...`;
  } else {
    st.textContent = d.error
      ? `scan started ${d.started} ended ${d.finished}: ${d.error}`
      : `scan ${d.started}–${d.finished}: ${d.samples} readings, ${d.segments.length} detection(s)`;
  }
  renderLaserScanResults(d);
  return d.running;
}

function renderLaserScanResults(d) {
  const box = document.getElementById('laserScanResults');
  if (!d.segments.length) {
    box.innerHTML = d.running ? '' : '<span style="font-size:12px">nothing detected</span>';
    return;
  }
  const teeth = d.offset_mm !== null && d.offset_mm !== undefined;
  const grip = !!d.gripper_offset;
  const range = (a, b) => a === b ? a.toFixed(2) : `${a.toFixed(2)} – ${b.toFixed(2)}`;
  const who = grip ? 'gripper' : 'laser';
  const fmtEdge = (g, side, v) => (g.open_edges || []).includes(side) ? `${v.toFixed(2)}+` : v.toFixed(2);
  let html = (grip
      ? `<div style="font-size:11px;color:var(--muted)">x/y are gripper positions (laser offset dx=${d.gripper_offset.dx}, ` +
        `dy=${d.gripper_offset.dy} mm)${d.segments.some(g => g.reachable === false) ? '; * = outside the gripper\'s travel' : ''}</div>`
      : '<div style="font-size:11px;color:var(--muted)">x/y are laser positions — save the laser stand position to get gripper positions</div>') +
    '<table style="font-size:12px;font-family:monospace"><thead><tr>' +
    `<td>${who} y mm</td><td>${who} x start</td><td>${who} x end</td><td>x length mm</td><td>± mm</td>` +
    (teeth ? '<td>from teeth mm</td>' : '') + '<td>laser mm</td></tr></thead><tbody>';
  for (const g of d.segments) {
    const mark = g.reachable === false ? ' *' : '';
    const err = g.edge_err_mm === undefined ? '' : (g.refined ? '' : '~') + g.edge_err_mm.toFixed(2);
    html += `<tr><td>${g.y.toFixed(1)}</td><td>${fmtEdge(g, 'low', g.x_from)}</td>` +
      `<td>${fmtEdge(g, 'high', g.x_to)}${mark}</td>` +
      `<td>${g.length_mm === undefined ? '' : g.length_mm.toFixed(2)}</td><td>${err}</td>` +
      (teeth ? `<td>${range(g.teeth_min, g.teeth_max)}</td>` : '') +
      `<td>${range(g.laser_min, g.laser_max)}</td></tr>`;
  }
  box.innerHTML = html + '</tbody></table>' +
    '<div style="font-size:11px;color:var(--muted)">± is per edge; ~ = fast sweep only (not refined yet); ' +
    '+ = the object runs past the end of travel, so that edge and the length are only a limit. ' +
    'Length is along x only — y is sampled every sweep step.</div>';
}

async function doLaserCheck() {
  const d = await fetch('/laser/info').then(r => r.json()).catch(e => ({ok: false, error: String(e)}));
  if (!d.ok) { laserStatus(d.error || 'check failed'); return; }
  laserStatus(d.is_laser
    ? `laser OK — ID ${d.id}, ${d.baud} baud, range ${d.range_mm[0]}–${d.range_mm[1]} mm`
    : `ID ${d.id} answered but reports ${d.baud} in the baud register (bus is ${d.bus_baud}) — may not be the laser`);
}

// ── Plate grids (tube plates and cap holder plates) ───────────────────────────
// Mirrors the Python _grid / _plate_corners / _plate_pos helpers. Each plate
// has its own grid: rows numbered 1..rows, columns lettered A.., pitch in mm.
// Plates saved before grids existed get the old fixed size with no pitch.
const PLATE_DEFAULT_GRID  = {rows: 12, cols: 8, row_pitch_mm: null, col_pitch_mm: null};
const HOLDER_DEFAULT_GRID = {rows: 8,  cols: 6, row_pitch_mm: null, col_pitch_mm: null};

function plateGrid(plate, def) {
  return Object.assign({}, def, (plate && plate.grid) || {});
}

function gridLetters(g) {
  return Array.from({length: g.cols}, (_, i) => String.fromCharCode(65 + i));
}

function gridRows(g) {
  return Array.from({length: g.rows}, (_, i) => i + 1);
}

// every position id, column by column (A1, A2, ... then B1, ...)
function gridPositions(g) {
  const ids = [];
  gridLetters(g).forEach(c => gridRows(g).forEach(r => ids.push(c + r)));
  return ids;
}

// [A<last row>, <last col><last row>, A1, <last col>1]
function gridCornerNames(g) {
  const lastC = gridLetters(g)[g.cols - 1];
  return [...new Set([`A${g.rows}`, `${lastC}${g.rows}`, 'A1', `${lastC}1`])];
}

function gridDescription(g) {
  const pitch = (g.row_pitch_mm && g.col_pitch_mm)
    ? `row pitch ${g.row_pitch_mm} mm, column pitch ${g.col_pitch_mm} mm`
    : 'no pitch set — calibrate all 4 corners';
  return `${g.rows} rows × ${g.cols} columns · ${pitch}`;
}

// The plate's 4 corners, or null if it can't be located yet. Calibrated
// corners are used as-is; missing ones are filled in from the nearest
// calibrated corner plus pitch (see _plate_corners in the Python side).
// Returns {corners: {name: {x,z,y}}, estimated: Set of filled-in names}.
function plateCornersFull(plate, def) {
  const g = plateGrid(plate, def);
  const saved = (plate && plate.corners) || {};
  const names = gridCornerNames(g);
  const lastC = gridLetters(g)[g.cols - 1];
  const idx = {};
  idx[`A${g.rows}`] = [0, g.rows - 1];
  idx[`${lastC}${g.rows}`] = [g.cols - 1, g.rows - 1];
  idx['A1'] = [0, 0];
  idx[`${lastC}1`] = [g.cols - 1, 0];
  const known = {};
  names.forEach(n => { if (saved[n]) known[n] = saved[n]; });
  const knownNames = Object.keys(known);
  if (knownNames.length === names.length) return {corners: known, estimated: new Set()};
  const cp = g.col_pitch_mm, rp = g.row_pitch_mm;
  if (!knownNames.length || !cp || !rp) return null;

  const nAR = `A${g.rows}`, nLR = `${lastC}${g.rows}`, nA1 = 'A1', nL1 = `${lastC}1`;
  function dir(pairs) {
    for (const [a, b] of pairs) {
      if (known[a] && known[b] && a !== b) {
        const dx = known[b].x - known[a].x, dy = known[b].y - known[a].y;
        const n = Math.hypot(dx, dy);
        if (n > 1e-6) return [dx / n, dy / n];
      }
    }
    return null;
  }
  let u = dir([[nA1, nL1], [nAR, nLR]]);   // along a row: A -> last column
  let v = dir([[nA1, nAR], [nL1, nLR]]);   // along a column: 1 -> last row
  if (!u && !v) { u = [1, 0]; v = [0, 1]; }
  else if (!u) u = [v[1], -v[0]];
  else if (!v) v = [-u[1], u[0]];

  const out = {}, estimated = new Set();
  names.forEach(n => {
    if (known[n]) { out[n] = known[n]; return; }
    const [ci, ri] = idx[n];
    let src = knownNames[0], best = Infinity;
    knownNames.forEach(k => {
      const d = Math.abs(idx[k][0] - ci) + Math.abs(idx[k][1] - ri);
      if (d < best) { best = d; src = k; }
    });
    const dc = ci - idx[src][0], dr = ri - idx[src][1], p = known[src];
    out[n] = {
      x: Math.round((p.x + dc * cp * u[0] + dr * rp * v[0]) * 100) / 100,
      z: p.z,
      y: Math.round((p.y + dc * cp * u[1] + dr * rp * v[1]) * 100) / 100,
    };
    estimated.add(n);
  });
  return {corners: out, estimated};
}

// x/z/y of one position by bilinear interpolation between the corners
function platePos(plate, def, id) {
  const g = plateGrid(plate, def);
  const ci = id.charCodeAt(0) - 65, ri = parseInt(id.slice(1)) - 1;
  if (!(ci >= 0 && ci < g.cols && ri >= 0 && ri < g.rows)) return null;
  const full = plateCornersFull(plate, def);
  if (!full) return null;
  const c = full.corners;
  const lastC = gridLetters(g)[g.cols - 1];
  const s = g.cols > 1 ? ci / (g.cols - 1) : 0;   // 0 = column A, 1 = last column
  const t = g.rows > 1 ? ri / (g.rows - 1) : 0;   // 0 = row 1, 1 = last row
  const pA1 = c['A1'], pL1 = c[`${lastC}1`], pAR = c[`A${g.rows}`], pLR = c[`${lastC}${g.rows}`];
  function bi(k) {
    return (1-s)*(1-t)*pA1[k] + s*(1-t)*pL1[k] + (1-s)*t*pAR[k] + s*t*pLR[k];
  }
  return {
    x: Math.round(bi('x') * 10) / 10,
    z: Math.round(bi('z') * 10) / 10,
    y: Math.round(bi('y') * 10) / 10,
  };
}

// Draws the grid of round position buttons: column letters across the
// top, row numbers down the side.
function buildPlateGrid(containerId, g, idPrefix, onClick) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  const grid = document.createElement('div');
  grid.className = 'well-plate';
  grid.style.gridTemplateColumns = `auto repeat(${g.cols}, 34px)`;

  grid.appendChild(document.createElement('div'));
  gridLetters(g).forEach(c => {
    const h = document.createElement('div');
    h.className = 'well-hdr';
    h.textContent = c;
    grid.appendChild(h);
  });

  gridRows(g).forEach(r => {
    const lbl = document.createElement('div');
    lbl.className = 'well-row-lbl';
    lbl.textContent = r;
    grid.appendChild(lbl);
    gridLetters(g).forEach(c => {
      const btn = document.createElement('button');
      btn.className = 'well';
      btn.id = idPrefix + c + r;
      btn.title = c + r;
      btn.onclick = () => onClick(c + r);
      grid.appendChild(btn);
    });
  });

  container.appendChild(grid);
}

function buildCornerButtons(containerId, g, idPrefix, onClick) {
  const box = document.getElementById(containerId);
  box.innerHTML = '';
  gridCornerNames(g).forEach(c => {
    const btn = document.createElement('button');
    btn.id = idPrefix + 'btn_' + c;
    btn.onclick = () => onClick(c);
    btn.appendChild(document.createTextNode(c + ' '));
    const ind = document.createElement('span');
    ind.id = idPrefix + 'ind_' + c;
    ind.textContent = '○';
    btn.appendChild(ind);
    box.appendChild(btn);
  });
}

// ● calibrated, ◐ estimated from pitch, ○ not calibrated
function showCornerStatus(idPrefix, plate, def) {
  const g = plateGrid(plate, def);
  const saved = (plate && plate.corners) || {};
  const full = plateCornersFull(plate, def);
  gridCornerNames(g).forEach(c => {
    const ind = document.getElementById(idPrefix + 'ind_' + c);
    if (!ind) return;
    if (saved[c]) {
      ind.textContent = '●'; ind.style.color = '#35d07f'; ind.title = 'calibrated';
    } else if (full && full.estimated.has(c)) {
      ind.textContent = '◐'; ind.style.color = '#ffb547'; ind.title = 'estimated from pitch';
    } else {
      ind.textContent = '○'; ind.style.color = '#a8a8a8'; ind.title = 'not calibrated';
    }
  });
}

function cornerStatusText(name, plate, def) {
  const saved = ((plate && plate.corners) || {})[name];
  if (saved) return `${name}: x=${saved.x}  z=${saved.z}  y=${saved.y}`;
  const full = plateCornersFull(plate, def);
  if (full && full.estimated.has(name)) {
    const p = full.corners[name];
    return `${name}: not calibrated — estimated from pitch: x=${p.x}  z=${p.z}  y=${p.y}`;
  }
  return `${name}: not calibrated`;
}

function toggleAddPlateForm(formId, show) {
  const form = document.getElementById(formId);
  form.style.display = show ? '' : 'none';
  if (show) form.querySelector('input[type=text]').focus();
}

// Reads the add-plate form; returns the request body, or a string error.
function readAddPlateForm(prefix) {
  const name = document.getElementById(prefix + 'Name').value.trim();
  if (!name) return 'enter a plate name';
  const rows = parseInt(document.getElementById(prefix + 'Rows').value);
  const cols = parseInt(document.getElementById(prefix + 'Cols').value);
  if (!(rows >= 1 && rows <= 99)) return 'rows must be 1-99';
  if (!(cols >= 1 && cols <= 26)) return 'columns must be 1-26 (one letter each)';
  const pitch = id => {
    const v = document.getElementById(id).value.trim();
    return v === '' ? null : parseFloat(v);
  };
  const row_pitch_mm = pitch(prefix + 'RowPitch');
  const col_pitch_mm = pitch(prefix + 'ColPitch');
  if ((row_pitch_mm !== null && !(row_pitch_mm > 0)) || (col_pitch_mm !== null && !(col_pitch_mm > 0)))
    return 'pitch must be more than 0 mm (or blank)';
  if ((row_pitch_mm === null) !== (col_pitch_mm === null))
    return 'enter both pitches, or leave both blank';
  return {plate: name, rows, cols, row_pitch_mm, col_pitch_mm};
}

// ── Tube plate grid ───────────────────────────────────────────────────────────
let _selectedWell    = null;
let _calibData       = {};   // current plate's calibrated corners — may be partial
let _selectedCorner  = null;
let _plates          = {};   // {plateName: {grid:{...}, corners:{...}, tubes:[...]}}
let _currentPlate    = null;
let _tubeMap         = new Set();
let _editTubeMode    = false;

function curPlate()     { return _plates[_currentPlate] || {}; }
function curPlateGrid() { return plateGrid(curPlate(), PLATE_DEFAULT_GRID); }

function populatePlateSelect(plates, selected) {
  const sel = document.getElementById('plateSelect');
  sel.innerHTML = '';
  Object.keys(plates).forEach(name => {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    if (name === selected) opt.selected = true;
    sel.appendChild(opt);
  });
}

function switchPlate(name) {
  _currentPlate = name;
  document.getElementById('calibPlateLabel').textContent = name;
  const plate = curPlate();
  const g = curPlateGrid();
  document.getElementById('plateGridInfo').textContent = gridDescription(g);
  _calibData = plate.corners || {};
  buildPlateGrid('wellGrid', g, 'well_', selectWell);
  buildCornerButtons('cornerBtns', g, 'c', selectCorner);
  populateRangeControls(g);
  const corners = gridCornerNames(g);
  selectCorner(corners.includes(_selectedCorner) ? _selectedCorner : corners[0]);
  _tubeMap = new Set(plate.tubes || []);
  refreshWellClasses();
  const keep = _selectedWell && gridPositions(g).includes(_selectedWell) ? _selectedWell : null;
  _selectedWell = null;
  document.getElementById('wellLabel').textContent = '—';
  document.getElementById('wellPos').textContent = '';
  if (keep) selectWell(keep);
  renderLoopPlates();
}

// checkbox list of plates included in the shuffle loop pool
function renderLoopPlates() {
  const box = document.getElementById('loopPlates');
  if (!box) return;
  const prev = new Set(getLoopPlates());   // preserve existing selection across re-renders
  box.innerHTML = '';
  Object.keys(_plates).forEach(name => {
    const label = document.createElement('label');
    label.style.cssText = 'font-size:12px;display:inline-flex;align-items:center;gap:3px;margin-right:8px';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = name;
    // keep prior checks; on first render default to the current plate only
    cb.checked = prev.size ? prev.has(name) : (name === _currentPlate);
    label.appendChild(cb);
    label.appendChild(document.createTextNode(name));
    box.appendChild(label);
  });
}

function getLoopPlates() {
  const box = document.getElementById('loopPlates');
  if (!box) return [];
  return Array.from(box.querySelectorAll('input[type=checkbox]:checked')).map(cb => cb.value);
}

function selectCorner(name) {
  _selectedCorner = name;
  gridCornerNames(curPlateGrid()).forEach(c => {
    const btn = document.getElementById('cbtn_' + c);
    if (btn) btn.classList.toggle('active', c === name);
  });
  const saved = _calibData[name];
  document.getElementById('calibX').value = saved ? saved.x : '';
  document.getElementById('calibZ').value = saved ? saved.z : '';
  document.getElementById('calibY').value = saved ? saved.y : '';
  document.getElementById('calibStatus').textContent = cornerStatusText(name, curPlate(), PLATE_DEFAULT_GRID);
  refreshCornerStatus();
}

function refreshCornerStatus() {
  showCornerStatus('c', curPlate(), PLATE_DEFAULT_GRID);
}

async function doAddPlate() {
  const body = readAddPlateForm('newPlate');
  if (typeof body === 'string') { appendLog([body]); return; }
  const d = await post('/calibration/add_plate', body)
    .catch(e => ({ok:false, error:String(e)}));
  if (!d.ok) { appendLog([d.error || 'add plate failed']); return; }
  _plates = d.calibration.plates;
  populatePlateSelect(_plates, body.plate);
  switchPlate(body.plate);
  document.getElementById('newPlateName').value = '';
  toggleAddPlateForm('addPlateForm', false);
}

async function doDeletePlate() {
  if (!_currentPlate) return;
  if (Object.keys(_plates).length <= 1) {
    appendLog(['cannot delete the last plate']); return;
  }
  if (!confirm(`Delete plate "${_currentPlate}"?`)) return;
  const d = await post('/calibration/delete_plate', {plate: _currentPlate})
    .catch(e => ({ok:false, error:String(e)}));
  if (!d.ok) { appendLog([d.error || 'delete failed']); return; }
  _plates = d.calibration.plates;
  const first = Object.keys(_plates)[0];
  populatePlateSelect(_plates, first);
  switchPlate(first);
}

// a named plate's saved data (empty object if unknown)
function plateData(name) {
  return _plates[name] || {};
}

// `plate` defaults to the current plate; pass another plate's data to
// compute a well position on a different plate.
function getWellPosition(id, plate) {
  return platePos(plate || curPlate(), PLATE_DEFAULT_GRID, id);
}

function selectWell(id) {
  if (_editTubeMode) {
    if (_tubeMap.has(id)) _tubeMap.delete(id);
    else _tubeMap.add(id);
    refreshWellClasses();
    saveTubes();
    return;
  }
  if (_selectedWell) {
    const prev = document.getElementById('well_' + _selectedWell);
    if (prev) prev.classList.remove('selected');
  }
  if (_selectedWell === id) {
    _selectedWell = null;
    document.getElementById('wellLabel').textContent = '—';
    document.getElementById('wellPos').textContent = '';
    return;
  }
  _selectedWell = id;
  document.getElementById('well_' + id).classList.add('selected');
  document.getElementById('wellLabel').textContent = id;
  const pos = getWellPosition(id);
  document.getElementById('wellPos').textContent = pos
    ? `x=${pos.x}  z=${pos.z}  y=${pos.y}`
    : '(calibrate all 4 corners, or one corner with pitch set)';
}

// Column dropdowns and row limits follow the current plate's size. A range
// that covered the whole previous plate covers the whole new one; any
// other range is kept where it still fits.
let _rangeGrid = null;
function populateRangeControls(g) {
  const letters = gridLetters(g);
  const wasFull = !_rangeGrid || getRangeWells(_rangeGrid).length === _rangeGrid.rows * _rangeGrid.cols;
  _rangeGrid = g;
  ['rangeColFrom','rangeColTo'].forEach((id, i) => {
    const sel = document.getElementById(id);
    const prev = wasFull ? null : sel.value;
    sel.innerHTML = '';
    letters.forEach(c => {
      const opt = document.createElement('option');
      opt.value = opt.textContent = c;
      sel.appendChild(opt);
    });
    sel.value = letters.includes(prev) ? prev : (i === 0 ? letters[0] : letters[letters.length - 1]);
  });
  ['rangeRowFrom','rangeRowTo'].forEach((id, i) => {
    const el = document.getElementById(id);
    el.max = g.rows;
    const v = parseInt(el.value);
    if (wasFull || !(v >= 1 && v <= g.rows)) el.value = i === 0 ? 1 : g.rows;
  });
}

function getRangeWells(g) {
  g = g || curPlateGrid();
  const letters = gridLetters(g);
  const colFrom = document.getElementById('rangeColFrom').value;
  const colTo   = document.getElementById('rangeColTo').value;
  const rowFrom = parseInt(document.getElementById('rangeRowFrom').value) || 1;
  const rowTo   = parseInt(document.getElementById('rangeRowTo').value)   || g.rows;
  const ci = letters.indexOf(colFrom);
  const cj = letters.indexOf(colTo);
  if (ci < 0 || cj < 0) return [];
  const colRange = letters.slice(Math.min(ci,cj), Math.max(ci,cj)+1);
  const rMin = Math.min(rowFrom, rowTo);
  const rMax = Math.max(rowFrom, rowTo);
  const wells = [];
  colRange.forEach(c => {
    for (let r = rMin; r <= rMax; r++) wells.push(c + r);
  });
  return wells;
}

function updateRangeHighlight() {
  const inRange = new Set(getRangeWells());
  gridPositions(curPlateGrid()).forEach(id => {
    const el = document.getElementById('well_' + id);
    if (el) el.classList.toggle('in-range', inRange.has(id));
  });
}

function refreshWellClasses() {
  const inRange = new Set(getRangeWells());
  gridPositions(curPlateGrid()).forEach(id => {
    const el = document.getElementById('well_' + id);
    if (!el) return;
    el.classList.toggle('has-tube', _tubeMap.has(id));
    el.classList.toggle('in-range', inRange.has(id));
  });
}

function toggleEditTubeMode() {
  _editTubeMode = !_editTubeMode;
  const btn  = document.getElementById('btnEditTubes');
  const hint = document.getElementById('editTubesHint');
  const grid = document.getElementById('wellGrid');
  if (_editTubeMode) {
    btn.textContent = 'done editing';
    btn.style.background = '#e07040';
    btn.style.color = '#fff';
    hint.style.display = '';
    grid.classList.add('edit-tube-active');
  } else {
    btn.textContent = 'edit tube layout';
    btn.style.background = '';
    btn.style.color = '';
    hint.style.display = 'none';
    grid.classList.remove('edit-tube-active');
  }
}

async function saveTubes() {
  if (!_currentPlate) return;
  const tubes = Array.from(_tubeMap);
  _plates[_currentPlate] = _plates[_currentPlate] || {};
  _plates[_currentPlate].tubes = tubes;
  await post('/calibration/save_tubes', {plate: _currentPlate, tubes})
    .catch(() => {});
}

function fillAllTubes() {
  if (!_currentPlate) return;
  gridPositions(curPlateGrid()).forEach(id => _tubeMap.add(id));
  refreshWellClasses();
  saveTubes();
}

function clearAllTubes() {
  if (!_currentPlate) return;
  _tubeMap.clear();
  refreshWellClasses();
  saveTubes();
}

// ── Tube cap holder grid ──────────────────────────────────────────────────────
let _capSelectedPos  = null;
let _capCalibData    = {};   // current holder plate's calibrated corners — may be partial
let _capSelectedCorner = null;
let _capPlates       = {};   // {plateName: {grid:{...}, corners:{...}, caps:[...]}}
let _currentCapPlate = null;
let _capMap          = new Set();
let _editCapMode     = false;

function curCapPlate()     { return _capPlates[_currentCapPlate] || {}; }
function curCapPlateGrid() { return plateGrid(curCapPlate(), HOLDER_DEFAULT_GRID); }

function populateCapPlateSelect(plates, selected) {
  const sel = document.getElementById('capPlateSelect');
  sel.innerHTML = '';
  Object.keys(plates).forEach(name => {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    if (name === selected) opt.selected = true;
    sel.appendChild(opt);
  });
}

function switchCapPlate(name) {
  _currentCapPlate = name;
  document.getElementById('capCalibPlateLabel').textContent = name;
  const plate = curCapPlate();
  const g = curCapPlateGrid();
  document.getElementById('capPlateGridInfo').textContent = gridDescription(g);
  _capCalibData = plate.corners || {};
  buildPlateGrid('capGrid', g, 'cap_', selectCapPos);
  buildCornerButtons('capCornerBtns', g, 'capc', selectCapCorner);
  const corners = gridCornerNames(g);
  selectCapCorner(corners.includes(_capSelectedCorner) ? _capSelectedCorner : corners[0]);
  _capMap = new Set(plate.caps || []);
  refreshCapClasses();
  const keep = _capSelectedPos && gridPositions(g).includes(_capSelectedPos) ? _capSelectedPos : null;
  _capSelectedPos = null;
  document.getElementById('capLabel').textContent = '—';
  document.getElementById('capPos').textContent = '';
  if (keep) selectCapPos(keep);
}

function selectCapCorner(name) {
  _capSelectedCorner = name;
  gridCornerNames(curCapPlateGrid()).forEach(c => {
    const btn = document.getElementById('capcbtn_' + c);
    if (btn) btn.classList.toggle('active', c === name);
  });
  const saved = _capCalibData[name];
  document.getElementById('capCalibX').value = saved ? saved.x : '';
  document.getElementById('capCalibZ').value = saved ? saved.z : '';
  document.getElementById('capCalibY').value = saved ? saved.y : '';
  document.getElementById('capCalibStatus').textContent = cornerStatusText(name, curCapPlate(), HOLDER_DEFAULT_GRID);
  refreshCapCornerStatus();
}

function refreshCapCornerStatus() {
  showCornerStatus('capc', curCapPlate(), HOLDER_DEFAULT_GRID);
}

async function doAddCapPlate() {
  const body = readAddPlateForm('newCapPlate');
  if (typeof body === 'string') { appendLog([body]); return; }
  const d = await post('/calibration/holder_plate/add', body)
    .catch(e => ({ok:false, error:String(e)}));
  if (!d.ok) { appendLog([d.error || 'add plate failed']); return; }
  _capPlates = d.calibration.holder_plates;
  populateCapPlateSelect(_capPlates, body.plate);
  switchCapPlate(body.plate);
  document.getElementById('newCapPlateName').value = '';
  toggleAddPlateForm('addCapPlateForm', false);
}

async function doDeleteCapPlate() {
  if (!_currentCapPlate) return;
  if (Object.keys(_capPlates).length <= 1) {
    appendLog(['cannot delete the last plate']); return;
  }
  if (!confirm(`Delete plate "${_currentCapPlate}"?`)) return;
  const d = await post('/calibration/holder_plate/delete', {plate: _currentCapPlate})
    .catch(e => ({ok:false, error:String(e)}));
  if (!d.ok) { appendLog([d.error || 'delete failed']); return; }
  _capPlates = d.calibration.holder_plates;
  const first = Object.keys(_capPlates)[0];
  populateCapPlateSelect(_capPlates, first);
  switchCapPlate(first);
}

function getCapPosition(id, plate) {
  return platePos(plate || curCapPlate(), HOLDER_DEFAULT_GRID, id);
}

function selectCapPos(id) {
  if (_editCapMode) {
    if (_capMap.has(id)) _capMap.delete(id);
    else _capMap.add(id);
    refreshCapClasses();
    saveCaps();
    return;
  }
  if (_capSelectedPos) {
    const prev = document.getElementById('cap_' + _capSelectedPos);
    if (prev) prev.classList.remove('selected');
  }
  if (_capSelectedPos === id) {
    _capSelectedPos = null;
    document.getElementById('capLabel').textContent = '—';
    document.getElementById('capPos').textContent = '';
    return;
  }
  _capSelectedPos = id;
  document.getElementById('cap_' + id).classList.add('selected');
  document.getElementById('capLabel').textContent = id;
  const pos = getCapPosition(id);
  document.getElementById('capPos').textContent = pos
    ? `x=${pos.x}  z=${pos.z}  y=${pos.y}`
    : '(calibrate all 4 corners, or one corner with pitch set)';
}

function refreshCapClasses() {
  gridPositions(curCapPlateGrid()).forEach(id => {
    const el = document.getElementById('cap_' + id);
    if (!el) return;
    el.classList.toggle('has-tube', _capMap.has(id));
  });
}

function toggleEditCapMode() {
  _editCapMode = !_editCapMode;
  const btn  = document.getElementById('btnEditCaps');
  const hint = document.getElementById('editCapsHint');
  const grid = document.getElementById('capGrid');
  if (_editCapMode) {
    btn.textContent = 'done editing';
    btn.style.background = '#e07040';
    btn.style.color = '#fff';
    hint.style.display = '';
    grid.classList.add('edit-tube-active');
  } else {
    btn.textContent = 'edit layout';
    btn.style.background = '';
    btn.style.color = '';
    hint.style.display = 'none';
    grid.classList.remove('edit-tube-active');
  }
}

async function saveCaps() {
  if (!_currentCapPlate) return;
  const caps = Array.from(_capMap);
  _capPlates[_currentCapPlate] = _capPlates[_currentCapPlate] || {};
  _capPlates[_currentCapPlate].caps = caps;
  await post('/calibration/holder_plate/save_caps', {plate: _currentCapPlate, caps})
    .catch(() => {});
}

function fillAllCaps() {
  if (!_currentCapPlate) return;
  gridPositions(curCapPlateGrid()).forEach(id => _capMap.add(id));
  refreshCapClasses();
  saveCaps();
}

function clearAllCaps() {
  if (!_currentCapPlate) return;
  _capMap.clear();
  refreshCapClasses();
  saveCaps();
}

async function doReadIntoCapCalib() {
  const r = await fetch('/positions').catch(() => null);
  if (!r) { document.getElementById('capCalibStatus').textContent = 'not connected'; return false; }
  const d = await r.json().catch(() => null);
  if (!d || !d.ok) {
    document.getElementById('capCalibStatus').textContent = d?.error || 'failed';
    return false;
  }
  const p = d.positions;
  if (p.x && p.x.val !== undefined && p.x.val !== null) document.getElementById('capCalibX').value = parseFloat(p.x.val.toFixed(2));
  if (p.z_gripper && p.z_gripper.val !== undefined && p.z_gripper.val !== null) document.getElementById('capCalibZ').value = parseFloat(p.z_gripper.val.toFixed(2));
  if (p.y && p.y.val !== undefined && p.y.val !== null) document.getElementById('capCalibY').value = parseFloat(p.y.val.toFixed(2));
  document.getElementById('capCalibStatus').textContent = 'position read — click save to store';
  return true;
}

async function doMoveToCapCalib() {
  const x = parseFloat(document.getElementById('capCalibX').value);
  const z = parseFloat(document.getElementById('capCalibZ').value);
  const y = parseFloat(document.getElementById('capCalibY').value);
  if ([x, z, y].some(isNaN)) {
    document.getElementById('capCalibStatus').textContent = 'enter x, z and y values first';
    return;
  }
  const d = await post('/extend/xyz', {x, y}).catch(e => ({ok:false, error:String(e)}));
  if (!d.ok) { document.getElementById('capCalibStatus').textContent = d.error || 'move failed'; return; }
  await waitUntilIdle();
  const dz = await post('/extend', {motor: 'z_gripper', mm: z}).catch(e => ({ok:false, error:String(e)}));
  if (!dz.ok) document.getElementById('capCalibStatus').textContent = dz.error || 'z move failed';
}

async function doSetCapCurrentPosition() {
  if (await doReadIntoCapCalib()) await doSaveCapCalib();
}

async function doSaveCapCalib() {
  const x = parseFloat(document.getElementById('capCalibX').value);
  const z = parseFloat(document.getElementById('capCalibZ').value);
  const y = parseFloat(document.getElementById('capCalibY').value);
  if ([x, z, y].some(isNaN)) {
    document.getElementById('capCalibStatus').textContent = 'enter x, z and y values first';
    return;
  }
  const d = await post('/calibration/holder_plate/save', {plate: _currentCapPlate, corner: _capSelectedCorner, x, z, y})
    .catch(e => ({ok:false, error:String(e)}));
  const el = document.getElementById('capCalibStatus');
  if (!d.ok) {
    el.textContent = d.error || 'save failed';
  } else {
    _capCalibData[_capSelectedCorner] = {x, z, y};
    _capPlates[_currentCapPlate] = _capPlates[_currentCapPlate] || {};
    _capPlates[_currentCapPlate].corners = _capCalibData;
    el.textContent = `${_capSelectedCorner} saved: x=${x}  z=${z}  y=${y}`;
    refreshCapCornerStatus();
    if (_capSelectedPos) { const p = _capSelectedPos; _capSelectedPos = null; selectCapPos(p); }
  }
}

async function doMoveToCap() {
  if (!_capSelectedPos) { appendLog(['select a position first']); return; }
  const pos = getCapPosition(_capSelectedPos);
  if (!pos) { appendLog(['calibrate all 4 corners, or one corner with pitch set']); return; }
  await post('/gripper/open').catch(() => {});
  const d = await post('/extend/xyz', {x: pos.x, y: pos.y})
    .catch(e => ({ok:false, error:String(e)}));
  if (!d.ok) { appendLog([d.error || 'move failed']); return; }
  await waitUntilIdle();
  const dz = await post('/extend', {motor: 'z_gripper', mm: pos.z})
    .catch(e => ({ok:false, error:String(e)}));
  if (!dz.ok) appendLog([dz.error || 'z move failed']);
}

// ── Tube shuffle loop ─────────────────────────────────────────────────────────
let _loopPollTimer = null;

async function doStartTubeLoop() {
  const grabMm  = parseFloat(document.getElementById('grabZ').value);
  const placeMm = parseFloat(document.getElementById('placeZ').value);
  if (isNaN(grabMm) || isNaN(placeMm)) {
    appendLog(['set grab and place z heights in the Control tab first']); return;
  }
  const iterVal = document.getElementById('loopIter').value.trim();
  const iterations = iterVal === '' ? 0 : parseInt(iterVal) || 0;

  const plates = getLoopPlates();
  if (plates.length === 0) { appendLog(['select at least one plate for the loop']); return; }

  const d = await post('/tube_loop/start', {
    plates,
    grab_z_mm: grabMm,
    place_z_mm: placeMm,
    iterations,
    allowed_wells: getRangeWells(),
  }).catch(e => ({ok:false, error:String(e)}));
  if (!d.ok) { appendLog([d.error || 'start loop failed']); return; }

  document.getElementById('btnLoopStart').style.display = 'none';
  document.getElementById('btnLoopStop').style.display  = '';
  document.getElementById('loopStatus').textContent = 'running…';
  _startLoopPoll();
}

async function doStopTubeLoop() {
  await post('/stop').catch(() => {});
}

function _startLoopPoll() {
  clearInterval(_loopPollTimer);
  _loopPollTimer = setInterval(async () => {
    const r = await fetch('/tube_loop/state').catch(() => null);
    if (!r) return;
    const d = await r.json().catch(() => null);
    if (!d || !d.ok) return;

    // sync tube map for all plates
    if (d.plates_tubes) {
      Object.keys(d.plates_tubes).forEach(name => {
        _plates[name] = _plates[name] || {};
        _plates[name].tubes = d.plates_tubes[name];
      });
      // refresh display for current plate
      _tubeMap = new Set(_plates[_currentPlate]?.tubes || []);
      refreshWellClasses();
    }

    const st = d.state;
    document.getElementById('loopStatus').textContent =
      `${st.status}${st.total > 0 ? ' / ' + st.total : ''}`;

    if (!st.running) {
      clearInterval(_loopPollTimer);
      _loopPollTimer = null;
      document.getElementById('btnLoopStart').style.display = '';
      document.getElementById('btnLoopStop').style.display  = 'none';
      document.getElementById('loopStatus').textContent = st.status;
    }
  }, 800);
}

async function loadCalibration() {
  const r = await fetch('/calibration').catch(() => null);
  if (!r) return;
  const d = await r.json().catch(() => null);
  if (!d || !d.ok) return;
  const cal = d.calibration;
  _plates = cal.plates || {'Plate 1': {}};
  const first = Object.keys(_plates)[0];
  populatePlateSelect(_plates, first);
  switchPlate(first);
  _capPlates = cal.holder_plates || {'Holder 1': {}};
  const firstCap = Object.keys(_capPlates)[0];
  populateCapPlateSelect(_capPlates, firstCap);
  switchCapPlate(firstCap);
  if (cal.grab_z_mm  !== undefined) document.getElementById('grabZ').value  = cal.grab_z_mm;
  if (cal.place_z_mm !== undefined) document.getElementById('placeZ').value = cal.place_z_mm;
  if (cal.decap_z_mm !== undefined) document.getElementById('decapZ').value = cal.decap_z_mm;
  if (cal.recap_z_mm !== undefined) document.getElementById('recapZ').value = cal.recap_z_mm;
  const holder = cal.tube_holder;
  if (holder) {
    document.getElementById('holderX').value = holder.x;
    document.getElementById('holderZ').value = holder.z;
    document.getElementById('holderY').value = holder.y;
    document.getElementById('holderStatus').textContent = `saved: x=${holder.x}  z=${holder.z}  y=${holder.y}`;
    document.getElementById('decapRecapHolderPos').textContent = `using tube holder position — x=${holder.x}  y=${holder.y}`;
  } else {
    document.getElementById('decapRecapHolderPos').textContent = 'tube holder not calibrated yet — set it in the "tube holder" tab';
  }
  const stand = cal.pipetting_stand;
  if (stand) {
    document.getElementById('standX').value = stand.x;
    document.getElementById('standZ').value = stand.z;
    document.getElementById('standY').value = stand.y;
    document.getElementById('standStatus').textContent = `saved: x=${stand.x}  z=${stand.z}  y=${stand.y}`;
  } else {
    document.getElementById('standStatus').textContent = 'not calibrated yet';
  }
  showLaserOffset(cal.laser_teeth_offset_mm);
  showLaserScanZ(cal.laser_scan_z_mm);
  showLaserStand(cal.laser_stand_xy, cal.pipetting_stand);
}

let _heightsSaveTimer = null;
function scheduleHeightsSave() {
  clearTimeout(_heightsSaveTimer);
  _heightsSaveTimer = setTimeout(() => {
    const grab  = parseFloat(document.getElementById('grabZ').value);
    const place = parseFloat(document.getElementById('placeZ').value);
    const decap = parseFloat(document.getElementById('decapZ').value);
    const recap = parseFloat(document.getElementById('recapZ').value);
    if (!isNaN(grab) && !isNaN(place) && !isNaN(decap) && !isNaN(recap))
      post('/calibration/save_heights', {grab_z_mm: grab, place_z_mm: place, decap_z_mm: decap, recap_z_mm: recap});
  }, 800);
}

async function doReadIntoCalib() {
  const r = await fetch('/positions').catch(() => null);
  if (!r) { document.getElementById('calibStatus').textContent = 'not connected'; return false; }
  const d = await r.json().catch(() => null);
  if (!d || !d.ok) {
    document.getElementById('calibStatus').textContent = d?.error || 'failed';
    return false;
  }
  const p = d.positions;
  if (p.x && p.x.val !== undefined && p.x.val !== null) document.getElementById('calibX').value = parseFloat(p.x.val.toFixed(2));
  if (p.z_gripper && p.z_gripper.val !== undefined && p.z_gripper.val !== null) document.getElementById('calibZ').value = parseFloat(p.z_gripper.val.toFixed(2));
  if (p.y && p.y.val !== undefined && p.y.val !== null) document.getElementById('calibY').value = parseFloat(p.y.val.toFixed(2));
  document.getElementById('calibStatus').textContent = 'position read — click save to store';
  return true;
}

async function doMoveToCalib() {
  const x = parseFloat(document.getElementById('calibX').value);
  const z = parseFloat(document.getElementById('calibZ').value);
  const y = parseFloat(document.getElementById('calibY').value);
  if ([x, z, y].some(isNaN)) {
    document.getElementById('calibStatus').textContent = 'enter x, z and y values first';
    return;
  }
  const d = await post('/extend/xyz', {x, y}).catch(e => ({ok:false, error:String(e)}));
  if (!d.ok) { document.getElementById('calibStatus').textContent = d.error || 'move failed'; return; }
  await waitUntilIdle();
  const dz = await post('/extend', {motor: 'z_gripper', mm: z}).catch(e => ({ok:false, error:String(e)}));
  if (!dz.ok) document.getElementById('calibStatus').textContent = dz.error || 'z move failed';
}

async function doSetCurrentPosition() {
  // One-click version of "read current position" + "save corner" — reads
  // the live x/z/y straight from the robot and saves it as the selected
  // corner for the current plate, without needing the two-step flow.
  if (await doReadIntoCalib()) await doSaveCalib();
}

async function doSaveCalib() {
  const x = parseFloat(document.getElementById('calibX').value);
  const z = parseFloat(document.getElementById('calibZ').value);
  const y = parseFloat(document.getElementById('calibY').value);
  if ([x, z, y].some(isNaN)) {
    document.getElementById('calibStatus').textContent = 'enter x, z and y values first';
    return;
  }
  const d = await post('/calibration/save', {plate: _currentPlate, corner: _selectedCorner, x, z, y})
    .catch(e => ({ok:false, error:String(e)}));
  const el = document.getElementById('calibStatus');
  if (!d.ok) {
    el.textContent = d.error || 'save failed';
  } else {
    _calibData[_selectedCorner] = {x, z, y};
    _plates[_currentPlate] = _plates[_currentPlate] || {};
    _plates[_currentPlate].corners = _calibData;
    el.textContent = `${_selectedCorner} saved: x=${x}  z=${z}  y=${y}`;
    refreshCornerStatus();
    if (_selectedWell) { const w = _selectedWell; _selectedWell = null; selectWell(w); }
  }
}

async function doReadIntoHolder() {
  const r = await fetch('/positions').catch(() => null);
  if (!r) { document.getElementById('holderStatus').textContent = 'not connected'; return false; }
  const d = await r.json().catch(() => null);
  if (!d || !d.ok) {
    document.getElementById('holderStatus').textContent = d?.error || 'failed';
    return false;
  }
  const p = d.positions;
  if (p.x && p.x.val !== undefined && p.x.val !== null) document.getElementById('holderX').value = parseFloat(p.x.val.toFixed(2));
  if (p.z_gripper && p.z_gripper.val !== undefined && p.z_gripper.val !== null) document.getElementById('holderZ').value = parseFloat(p.z_gripper.val.toFixed(2));
  if (p.y && p.y.val !== undefined && p.y.val !== null) document.getElementById('holderY').value = parseFloat(p.y.val.toFixed(2));
  document.getElementById('holderStatus').textContent = 'position read — click save to store';
  return true;
}

async function doMoveToHolder() {
  const x = parseFloat(document.getElementById('holderX').value);
  const z = parseFloat(document.getElementById('holderZ').value);
  const y = parseFloat(document.getElementById('holderY').value);
  if ([x, z, y].some(isNaN)) {
    document.getElementById('holderStatus').textContent = 'enter x, z and y values first';
    return;
  }
  const d = await post('/extend/xyz', {x, y}).catch(e => ({ok:false, error:String(e)}));
  if (!d.ok) { document.getElementById('holderStatus').textContent = d.error || 'move failed'; return; }
  await waitUntilIdle();
  const dz = await post('/extend', {motor: 'z_gripper', mm: z}).catch(e => ({ok:false, error:String(e)}));
  if (!dz.ok) document.getElementById('holderStatus').textContent = dz.error || 'z move failed';
}

async function doSaveHolder() {
  const x = parseFloat(document.getElementById('holderX').value);
  const z = parseFloat(document.getElementById('holderZ').value);
  const y = parseFloat(document.getElementById('holderY').value);
  if ([x, z, y].some(isNaN)) {
    document.getElementById('holderStatus').textContent = 'enter x, z and y values first';
    return;
  }
  const d = await post('/calibration/save_tube_holder', {x, z, y}).catch(e => ({ok:false, error:String(e)}));
  const el = document.getElementById('holderStatus');
  el.textContent = d.ok ? `saved: x=${x}  z=${z}  y=${y}` : (d.error || 'save failed');
  if (d.ok) document.getElementById('decapRecapHolderPos').textContent = `using tube holder position — x=${x}  y=${y}`;
}

async function doSetHolderCurrentPosition() {
  // One-click version of "read current position" + "save" — reads the live
  // x/z/y straight from the robot and saves it as the tube holder position,
  // without needing the two-step flow. Same pattern as doSetCurrentPosition
  // for plate corners.
  if (await doReadIntoHolder()) await doSaveHolder();
}

// ── Pipetting stand — single x/z/y position, same flow as the tube holder ──
async function doReadIntoStand() {
  const r = await fetch('/positions').catch(() => null);
  if (!r) { document.getElementById('standStatus').textContent = 'not connected'; return false; }
  const d = await r.json().catch(() => null);
  if (!d || !d.ok) {
    document.getElementById('standStatus').textContent = d?.error || 'failed';
    return false;
  }
  const p = d.positions;
  if (p.x && p.x.val !== undefined && p.x.val !== null) document.getElementById('standX').value = parseFloat(p.x.val.toFixed(2));
  if (p.z_gripper && p.z_gripper.val !== undefined && p.z_gripper.val !== null) document.getElementById('standZ').value = parseFloat(p.z_gripper.val.toFixed(2));
  if (p.y && p.y.val !== undefined && p.y.val !== null) document.getElementById('standY').value = parseFloat(p.y.val.toFixed(2));
  document.getElementById('standStatus').textContent = 'position read — click save to store';
  return true;
}

async function doMoveToStand() {
  const x = parseFloat(document.getElementById('standX').value);
  const z = parseFloat(document.getElementById('standZ').value);
  const y = parseFloat(document.getElementById('standY').value);
  if ([x, z, y].some(isNaN)) {
    document.getElementById('standStatus').textContent = 'enter x, z and y values first';
    return;
  }
  const d = await post('/extend/xyz', {x, y}).catch(e => ({ok:false, error:String(e)}));
  if (!d.ok) { document.getElementById('standStatus').textContent = d.error || 'move failed'; return; }
  await waitUntilIdle();
  const dz = await post('/extend', {motor: 'z_gripper', mm: z}).catch(e => ({ok:false, error:String(e)}));
  if (!dz.ok) document.getElementById('standStatus').textContent = dz.error || 'z move failed';
}

async function doSaveStand() {
  const x = parseFloat(document.getElementById('standX').value);
  const z = parseFloat(document.getElementById('standZ').value);
  const y = parseFloat(document.getElementById('standY').value);
  if ([x, z, y].some(isNaN)) {
    document.getElementById('standStatus').textContent = 'enter x, z and y values first';
    return;
  }
  const d = await post('/calibration/save_pipetting_stand', {x, z, y}).catch(e => ({ok:false, error:String(e)}));
  document.getElementById('standStatus').textContent = d.ok ? `saved: x=${x}  z=${z}  y=${y}` : (d.error || 'save failed');
}

async function doSetStandCurrentPosition() {
  // read the live x/z/y and save it as the pipetting stand in one click
  if (await doReadIntoStand()) await doSaveStand();
}

let ws = null;

async function post(url, body){
  const r = await fetch(url, {
    method:'POST',
    headers: body ? {'Content-Type':'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  return r.json();
}

// /extend/xyz (x+y only, see doExtend) returns immediately once its
// background task starts — it doesn't wait for the move to finish. Used
// after that call, before firing a follow-up z_gripper move, so the two
// don't race (moving z into position before x+y has actually arrived).
async function waitUntilIdle(pollMs = 300){
  while (true) {
    const d = await fetch('/state').then(r => r.json()).catch(() => null);
    if (!d || !d.busy) return;
    await new Promise(res => setTimeout(res, pollMs));
  }
}

function appendLog(lines){
  if(!lines || !lines.length) return;
  const el = document.getElementById('log');
  lines.forEach(l => {
    const d = document.createElement('div');
    d.textContent = l;
    el.appendChild(d);
  });
  el.scrollTop = el.scrollHeight;
}

// ── Pipettor tab (SOCON/SOPA ADP, ported from PiP_A/pipettor_control.py) ───────
// Talks to /pipettor/* — a separate device/connection from the x/y/z motor
// bus above, so it's namespaced with a "pip" prefix throughout and never
// touches connSection/ws/etc.

let pipAutoPollTimer = null;
let pipCameraStream = null;

function pipNumVal(id) { return parseFloat(document.getElementById(id).value); }

function pipSetConnStatus(msg, cls) {
  const el = document.getElementById('pipConnStatus');
  el.textContent = msg;
  el.className = cls;
}

async function pipRefreshPorts() {
  const res = await fetch('/pipettor/ports');
  const data = await res.json();
  const sel = document.getElementById('pipPort');
  const prev = sel.value;
  sel.innerHTML = '<option value="auto">auto (find it)</option>';
  if (!data.pyserial_available) {
    sel.innerHTML = '<option value="">pyserial not installed</option>';
    return;
  }
  data.ports.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.device;
    opt.textContent = p.device + (p.description ? ' - ' + p.description : '');
    sel.appendChild(opt);
  });
  sel.value = [...sel.options].some(o => o.value === prev) ? prev : 'auto';
}

async function pipConnect() {
  document.getElementById('pipBtnConn').disabled = true;
  const body = {
    port: document.getElementById('pipPort').value,
    baud: parseInt(document.getElementById('pipBaud').value),
    address: parseInt(document.getElementById('pipAddress').value),
    head: document.getElementById('pipHead').value,
    simulate: document.getElementById('pipSimulate').checked,
  };
  const d = await post('/pipettor/connect', body).catch(e => ({ok:false, error:String(e)}));
  document.getElementById('pipBtnConn').disabled = false;
  if (!d.ok) {
    pipSetConnStatus(d.error || 'connect failed', 'err');
    return;
  }
  pipSetConnStatus('connected' + (body.simulate ? ' (simulated)' : ' — ' + d.info.port), 'ok');
  document.getElementById('pipBtnConn').disabled = true;
  document.getElementById('pipBtnDisc').disabled = false;
  pipStartAutoPollIfNeeded();
}

async function pipDisconnect() {
  if (pipAutoPollTimer) { clearInterval(pipAutoPollTimer); pipAutoPollTimer = null; }
  await post('/pipettor/disconnect');
  pipSetConnStatus('disconnected', 'warn');
  document.getElementById('pipBtnConn').disabled = false;
  document.getElementById('pipBtnDisc').disabled = true;
}

async function pipCmd(action, params) {
  params = params || {};
  const d = await post('/pipettor/command', {action, params}).catch(e => ({ok:false, error:String(e)}));
  if (d.ok) {
    const r = d.result;
    pipUpdateStatusFromResult(r);
  } else {
    appendLog([`pipettor ${action} failed: ${d.error}`]);
  }
  pipRefreshLog();
  return d;
}

function pipUpdateActionStatus(label, r) {
  document.getElementById('pipActAction').textContent = label;
  document.getElementById('pipActState').textContent = r.busy ? 'busy' : (r.ok ? 'finished ok' : 'error');
  document.getElementById('pipActState').className = r.busy ? 'warn' : (r.ok ? 'ok' : 'err');
  document.getElementById('pipActCode').textContent = '0x' + r.error_code.toString(16).toUpperCase().padStart(2,'0') + ' (' + r.error_name + ')';
  document.getElementById('pipActDataAscii').textContent = r.data_ascii || '—';
  document.getElementById('pipActDataHex').textContent = r.data_hex || '—';
}

async function pipAspirate() {
  const d = await pipCmd('aspirate_full', {
    volume_ul: pipNumVal('pipAspVol'),
    max_speed: pipNumVal('pipAspMax'),
    start_speed: pipNumVal('pipAspStart'),
    acceleration: pipNumVal('pipAspAccel'),
    check_liquid: document.getElementById('pipAspCheckLiquid').checked,
  });
  if (d.ok) pipUpdateActionStatus('aspirate', d.result);
  return d;
}

async function pipDispense() {
  const d = await pipCmd('dispense_full', {
    volume_ul: pipNumVal('pipDispVol'),
    max_speed: pipNumVal('pipDispMax'),
    start_speed: pipNumVal('pipDispStart'),
    acceleration: pipNumVal('pipDispAccel'),
  });
  if (d.ok) pipUpdateActionStatus('dispense', d.result);
  return d;
}

async function pipStartDetection() {
  await pipCmd('set_lld_speed', {v: pipNumVal('pipLldSpeed')});
  const d = await pipCmd('start_level_detection', {sensitivity: pipNumVal('pipLldSens')});
  if (d.ok) pipUpdateActionStatus('start detection', d.result);
  return d;
}

async function pipCheckTip() {
  const d = await pipCmd('check_tip_mounted');
  if (d.ok) {
    const mounted = d.result.data_ascii.replace(/\.+$/,'').trim() === '1';
    document.getElementById('pipTipStatus').textContent = mounted ? 'tip mounted' : 'no tip mounted';
  }
}

async function pipRefreshADP() {
  const res = await fetch('/pipettor/refresh_adp');
  const data = await res.json();
  if (!data.ok) { appendLog([`pipettor refresh failed: ${data.error}`]); return; }
  const v = data.values;
  if (v.empty_threshold !== null) document.getElementById('pipThEmpty').value = v.empty_threshold;
  if (v.foam_threshold !== null) document.getElementById('pipThFoam').value = v.foam_threshold;
  if (v.block_threshold !== null) document.getElementById('pipThBlock').value = v.block_threshold;
  if (v.cal_ratio !== null) document.getElementById('pipCalRatio').value = v.cal_ratio;
  if (v.cal_offset !== null) document.getElementById('pipCalOffset').value = v.cal_offset;
  pipRefreshLog();
}

function pipUpdateStatusFromResult(r) {
  document.getElementById('pipLastErrCode').textContent = '0x' + r.error_code.toString(16).toUpperCase().padStart(2,'0');
  document.getElementById('pipLastErrText').textContent = r.error_name;
  document.getElementById('pipLastRaw').textContent = r.raw_recv_hex;
  pipSetConnStatus(r.busy ? 'busy' : (r.ok ? 'connected' : 'error'), r.busy ? 'warn' : (r.ok ? 'ok' : 'err'));
}

async function pipGetFirmware() {
  const d = await pipCmd('report_firmware_version');
  if (d.ok) document.getElementById('pipFwVersion').textContent = d.result.data_ascii.replace(/\.+$/,'') || d.result.data_hex;
}

async function pipPollStatus() {
  const res = await fetch('/pipettor/status');
  const data = await res.json();
  if (!data.connected) { pipSetConnStatus('disconnected', 'warn'); return; }
  if (data.result) pipUpdateStatusFromResult(data.result);
}

function pipToggleAutoPoll() { pipStartAutoPollIfNeeded(); }

function pipStartAutoPollIfNeeded() {
  if (pipAutoPollTimer) { clearInterval(pipAutoPollTimer); pipAutoPollTimer = null; }
  if (document.getElementById('pipAutoPoll').checked) {
    pipAutoPollTimer = setInterval(pipPollStatus, 2000);
  }
}

async function pipRefreshLog() {
  const res = await fetch('/pipettor/log?n=40');
  const data = await res.json();
  const el = document.getElementById('pipCommLog');
  el.textContent = data.log.map(e => {
    const t = new Date(e.ts * 1000).toLocaleTimeString();
    return `[${t}] addr=${e.address} cmd=${e.command}  sent=${e.sent_hex}  recv=${e.recv_hex}`;
  }).join('\n');
  el.scrollTop = el.scrollHeight;
}

async function pipToggleCamera() {
  const btn = document.getElementById('pipCamToggleBtn');
  const video = document.getElementById('pipCamVideo');
  const captureBtn = document.getElementById('pipCamCaptureBtn');

  if (pipCameraStream) {
    pipCameraStream.getTracks().forEach(t => t.stop());
    pipCameraStream = null;
    video.srcObject = null;
    video.style.display = 'none';
    captureBtn.style.display = 'none';
    btn.textContent = 'open camera';
    return;
  }

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    appendLog(['camera API not available (needs a secure/localhost origin)']);
    return;
  }
  try {
    pipCameraStream = await navigator.mediaDevices.getUserMedia({ video: true });
  } catch (e) {
    appendLog(['camera access failed: ' + e.message]);
    return;
  }
  video.srcObject = pipCameraStream;
  video.style.display = 'block';
  captureBtn.style.display = 'inline-block';
  btn.textContent = 'stop camera';
}

function pipCaptureSnapshot() {
  const video = document.getElementById('pipCamVideo');
  if (!pipCameraStream || !video.videoWidth) {
    appendLog(['camera not ready yet']);
    return;
  }
  const canvas = document.getElementById('pipCamCanvas');
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0);
  const img = document.getElementById('pipCamSnapshot');
  img.src = canvas.toDataURL('image/png');
  img.style.display = 'block';
}

window.addEventListener('beforeunload', () => {
  if (pipCameraStream) { pipCameraStream.getTracks().forEach(t => t.stop()); pipCameraStream = null; }
});

function pipApplySavedSettings(s) {
  const sel = document.getElementById('pipPort');
  if (s.port && [...sel.options].some(o => o.value === s.port)) sel.value = s.port;
  if (s.baud) document.getElementById('pipBaud').value = String(s.baud);
  if (s.head) document.getElementById('pipHead').value = s.head;
  if (s.address) document.getElementById('pipAddress').value = s.address;
}

(async function pipInit() {
  await pipRefreshPorts();
  const res = await fetch('/pipettor/settings');
  const s = await res.json();
  pipApplySavedSettings(s);
  pipRefreshLog();
  setInterval(pipRefreshLog, 4000);
})();

async function doLogin(){
  const name    = document.getElementById('userName').value.trim();
  const machine = document.getElementById('machineId').value.trim();
  const status  = document.getElementById('loginStatus');
  if(!name)    { status.textContent = 'enter a name';       status.className='err'; return; }
  if(!machine) { status.textContent = 'enter a machine ID'; status.className='err'; return; }
  const d = await post('/login', {user: name, machine}).catch(e => ({ok:false, error:String(e)}));
  if(!d.ok){
    status.textContent = d.error || 'login failed';
    status.className = 'err';
    return;
  }
  document.getElementById('loginSection').style.display    = 'none';
  document.getElementById('connSection').style.display     = '';
  document.getElementById('sessSection').style.display     = '';
  document.getElementById('notePostSection').style.display = '';
  document.getElementById('currentUser').textContent   = d.user;
  document.getElementById('currentMachine').textContent = d.machine;
  document.getElementById('tab_tube').style.display = '';
  document.getElementById('tab_holder').style.display = '';
  document.getElementById('tab_stand').style.display = '';
  document.getElementById('tab_capholder').style.display = '';
  document.getElementById('tab_pipettor').style.display = '';
  document.getElementById('tab_laser').style.display = '';
  loadSessions();
  loadCalibration();
  showReadme();
}

async function doLogout(){
  clearInterval(_loopPollTimer);
  _loopPollTimer = null;
  stopWs();
  await post('/logout');
  setConnStatus('disconnected', 'warn');
  document.getElementById('btnConn').disabled = false;
  document.getElementById('btnDisc').disabled = true;
  ['statusSection','ctrlSection','gripSection','grabPlaceSection','logSection'].forEach(
    id => document.getElementById(id).style.display = 'none');
  document.getElementById('connSection').style.display     = 'none';
  document.getElementById('sessSection').style.display     = 'none';
  document.getElementById('notePostSection').style.display = 'none';
  document.getElementById('loginSection').style.display    = '';
  document.getElementById('loginStatus').textContent = '';
  document.getElementById('userName').value  = '';
  document.getElementById('machineId').value = '';
  document.getElementById('tab_tube').style.display = 'none';
  document.getElementById('tab_holder').style.display = 'none';
  document.getElementById('tab_stand').style.display = 'none';
  document.getElementById('tab_capholder').style.display = 'none';
  document.getElementById('tab_pipettor').style.display = 'none';
  document.getElementById('tab_laser').style.display = 'none';
  showTab('control');
  loadSessions();
}

async function doConnect(){
  document.getElementById('btnConn').disabled = true;
  const d = await post('/connect', {
    port:     document.getElementById('port').value,
    baudrate: parseInt(document.getElementById('baud').value),
  }).catch(e => ({ok:false, error:String(e)}));
  document.getElementById('btnConn').disabled = false;
  if(!d.ok){
    setConnStatus(d.error || 'failed', 'err');
    return;
  }
  setConnStatus(`connected — motor bus ${d.port}, static gripper ${d.gripper_port || 'not found'}`, 'ok');
  document.getElementById('btnConn').disabled = true;
  document.getElementById('btnDisc').disabled = false;
  ['statusSection','ctrlSection','gripSection','grabPlaceSection','logSection'].forEach(
    id => document.getElementById(id).style.display = '');
  startWs();
  // auto home all on connect (the server skips it while TubeGrip holds a cap)
  document.getElementById('homeMotor').value = 'all';
  doHome(true);
}

async function doDisconnect(){
  stopWs();
  await post('/disconnect');
  setConnStatus('disconnected', 'warn');
  document.getElementById('btnConn').disabled = false;
  document.getElementById('btnDisc').disabled = true;
  setManualModeUi(false);
  ['statusSection','ctrlSection','gripSection','grabPlaceSection','logSection'].forEach(
    id => document.getElementById(id).style.display = 'none');
}

function mdToHtml(md){
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  let html = '';
  const lines = md.split('\n');
  let inPre = false, inTable = false, inList = false;
  for(let i = 0; i < lines.length; i++){
    let l = lines[i];
    if(l.startsWith('```')){
      if(inPre){ html += '</pre>'; inPre = false; }
      else { if(inList){html+='</ul>';inList=false;} html += '<pre>'; inPre = true; }
      continue;
    }
    if(inPre){ html += esc(l) + '\n'; continue; }
    if(l.startsWith('| ')){
      if(!inTable){ if(inList){html+='</ul>';inList=false;} html += '<table><thead>'; inTable='head'; }
      else if(inTable==='head' && l.match(/^\|[-| ]+\|/)){ html += '</thead><tbody>'; inTable='body'; continue; }
      const cells = l.split('|').slice(1,-1).map(c => `<td>${esc(c.trim())}</td>`).join('');
      html += `<tr>${cells}</tr>`;
      continue;
    }
    if(inTable){ html += inTable==='body'?'</tbody>':'' ; html += '</table>'; inTable=false; }
    if(l.startsWith('# ')){ if(inList){html+='</ul>';inList=false;} html += `<h2>${esc(l.slice(2))}</h2>`; continue; }
    if(l.startsWith('## ')){ if(inList){html+='</ul>';inList=false;} html += `<h3>${esc(l.slice(3))}</h3>`; continue; }
    if(l.startsWith('### ')){ if(inList){html+='</ul>';inList=false;} html += `<h3>${esc(l.slice(4))}</h3>`; continue; }
    if(l.match(/^---+$/)){ if(inList){html+='</ul>';inList=false;} html += '<hr>'; continue; }
    if(l.startsWith('- ')){
      if(!inList){ html += '<ul>'; inList=true; }
      l = l.slice(2);
      l = l.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/`(.+?)`/g,'<code>$1</code>');
      html += `<li>${l}</li>`; continue;
    }
    if(inList){ html += '</ul>'; inList = false; }
    if(l.trim() === ''){ html += '<br>'; continue; }
    l = l.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/`(.+?)`/g,'<code>$1</code>');
    html += `<p style="margin:3px 0">${l}</p>`;
  }
  if(inPre) html += '</pre>';
  if(inTable) html += '</tbody></table>';
  if(inList) html += '</ul>';
  return html;
}

let _readmeCache = null;
async function showReadme(){
  document.getElementById('readmeModal').classList.add('active');
  if(_readmeCache){
    document.getElementById('readmeContent').innerHTML = _readmeCache;
    return;
  }
  const r = await fetch('/readme').catch(()=>null);
  if(!r){ document.getElementById('readmeContent').textContent = 'Could not load README.'; return; }
  const d = await r.json();
  _readmeCache = mdToHtml(d.content);
  document.getElementById('readmeContent').innerHTML = _readmeCache;
}

function hideReadme(){
  document.getElementById('readmeModal').classList.remove('active');
}

async function doPostNote(){
  const text = document.getElementById('noteText').value.trim();
  if(!text) return;
  const d = await post('/notes', {text}).catch(e => ({ok:false, error:String(e)}));
  if(!d.ok){ appendLog([d.error || 'post note failed']); return; }
  document.getElementById('noteText').value = '';
  loadNotes();
}

async function loadNotes(){
  const r = await fetch('/notes').catch(()=>null);
  if(!r) return;
  const d = await r.json();
  const tbody = document.getElementById('notesBody');
  tbody.innerHTML = '';
  const notes = (d.notes || []).slice().reverse();
  if(!notes.length){
    tbody.innerHTML = '<tr><td colspan="3" style="color:var(--muted)">no notes yet</td></tr>';
    return;
  }
  notes.forEach(n => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td style="font-weight:bold">${n.user}</td>` +
      `<td>${n.machine || '—'}</td>` +
      `<td style="color:var(--muted)">${n.time}</td>` +
      `<td style="white-space:pre-wrap">${n.text}</td>`;
    tbody.appendChild(tr);
  });
}

async function loadRecentLogins(){
  const r = await fetch('/sessions').catch(()=>null);
  if(!r) return;
  const d = await r.json();
  const tbody = document.getElementById('recentBody');
  tbody.innerHTML = '';
  const sessions = (d.sessions || []).slice(-5).reverse();
  if(!sessions.length){
    tbody.innerHTML = '<tr><td colspan="4" style="color:var(--muted)">no sessions recorded</td></tr>';
    return;
  }
  sessions.forEach(s => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td>${s.user}</td>` +
      `<td>${s.machine || '—'}</td>` +
      `<td>${s.login}</td>` +
      `<td>${s.logout || '—'}</td>` +
      `<td>${s.duration_min !== undefined ? s.duration_min : '—'}</td>`;
    tbody.appendChild(tr);
  });
}

async function loadErrors(){
  const r = await fetch('/errors').catch(()=>null);
  if(!r) return;
  const d = await r.json();
  const tbody = document.getElementById('errorBody');
  tbody.innerHTML = '';
  const errors = (d.errors || []).slice().reverse();
  if(!errors.length){
    tbody.innerHTML = '<tr><td colspan="5" style="color:var(--muted)">no errors recorded</td></tr>';
    return;
  }
  errors.forEach(e => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td>${e.date}</td>` +
      `<td>${e.user}</td>` +
      `<td>${e.machine || '—'}</td>` +
      `<td>${e.time}</td>` +
      `<td style="font-weight:bold">${e.motor}</td>` +
      `<td class="err">${e.description}</td>`;
    tbody.appendChild(tr);
  });
}

async function loadSessions(){
  const r = await fetch('/sessions').catch(()=>null);
  if(!r) return;
  const d = await r.json();
  const tbody = document.getElementById('sessBody');
  tbody.innerHTML = '';
  const sessions = (d.sessions || []).slice().reverse();
  sessions.forEach((s, idx) => {
    const ops    = s.operations || [];
    const errs   = s.errors     || [];
    const hasDetail = ops.length || errs.length;
    const detId  = `sess_det_${idx}`;

    const tr = document.createElement('tr');
    tr.style.cursor = hasDetail ? 'pointer' : '';
    tr.innerHTML =
      `<td>${s.user}</td>` +
      `<td>${s.login}</td>` +
      `<td>${s.logout || '—'}</td>` +
      `<td>${s.duration_min !== undefined ? s.duration_min : '—'}</td>` +
      `<td>${ops.length ? `<span style="color:var(--green)">${ops.length}</span>` : '0'}</td>` +
      `<td>${errs.length ? `<span class="err">${errs.length}</span>` : '0'}</td>`;
    if(hasDetail){
      tr.onclick = () => {
        const det = document.getElementById(detId);
        det.style.display = det.style.display === 'none' ? '' : 'none';
      };
    }
    tbody.appendChild(tr);

    if(hasDetail){
      const detRow = document.createElement('tr');
      detRow.id = detId;
      detRow.style.display = 'none';
      let html = '<td colspan="6" style="padding:6px 12px;background:var(--panel-head)">';
      if(ops.length){
        html += '<div style="margin-bottom:4px;font-weight:bold">Operations</div>';
        ops.forEach(o => {
          html += `<div style="font-size:11px;padding:1px 0">${o.time} &nbsp; ${o.command}</div>`;
        });
      }
      if(errs.length){
        html += '<div style="margin:6px 0 4px;font-weight:bold;color:var(--red-text)">Errors</div>';
        errs.forEach(e => {
          html += `<div style="font-size:11px;padding:1px 0;color:var(--red-text)">${e.time} &nbsp; [${e.motor}] &nbsp; ${e.description}</div>`;
        });
      }
      html += '</td>';
      detRow.innerHTML = html;
      tbody.appendChild(detRow);
    }
  });
}

function setConnStatus(msg, cls){
  const el = document.getElementById('connStatus');
  el.textContent = msg;
  el.className = cls;
}

async function doHome(auto){
  const d = await post('/home', {motor: document.getElementById('homeMotor').value, auto: !!auto});
  if(!d.ok) appendLog([d.error || 'home failed']);
}

// z gripper / z pipettor — independent single-axis actuators, driven
// through the same generic /home and /extend endpoints as x/y/z (not part
// of the coordinated /extend/xyz gantry move).
async function doShowcaseStep(step){
  const d = await post('/showcase_decap/' + step).catch(e => ({ok:false, error:String(e)}));
  if(!d.ok) appendLog([d.error || `showcase decap ${step} failed`]);
}

const _SHOWCASE_HINTS = {
  idle:          'place the tube in the tube holder, then press begin',
  running:       'working…',
  await_finish:  'tube is open — press finished when done',
  await_release: 'press release tube to open the static gripper',
};

async function doShowcaseLoop(){
  const raw = document.getElementById('showcaseLoopCycles').value.trim();
  const cycles = raw === '' ? null : parseInt(raw, 10);
  if(cycles !== null && (isNaN(cycles) || cycles < 1)){ appendLog(['loop decap: cycles must be at least 1, or blank for ∞']); return; }
  const d = await post('/showcase_decap/loop', {cycles}).catch(e => ({ok:false, error:String(e)}));
  if(!d.ok) appendLog([d.error || 'loop decap failed']);
}

function setShowcaseUi(phase, loop, last){
  document.getElementById('showcaseLoopCount').textContent = loop ? `cycle ${loop}` : (last ? `last loop: ${last}` : '');
  document.getElementById('btnShowcaseBegin').style.display   = phase === 'idle' ? '' : 'none';
  document.getElementById('btnShowcaseFinish').style.display  = phase === 'await_finish' ? '' : 'none';
  document.getElementById('btnShowcaseRelease').style.display = phase === 'await_release' ? '' : 'none';
  document.getElementById('btnShowcaseLoop').disabled = phase !== 'idle';
  document.getElementById('showcaseHint').textContent = loop ? `loop decap — cycle ${loop}` : (_SHOWCASE_HINTS[phase] || '');
}

async function doHomeSingle(motor){
  const d = await post('/home', {motor});
  if(!d.ok) appendLog([d.error || `home ${motor} failed`]);
}

async function doExtendSingle(motor, inputId){
  const raw = document.getElementById(inputId).value;
  const d = await post('/extend', {motor, mm: raw !== '' ? parseFloat(raw) : null});
  if(!d.ok) appendLog([d.error || `extend ${motor} failed`]);
}

async function doExtend(){
  const x = document.getElementById('extX').value;
  const y = document.getElementById('extY').value;
  const d = await post('/extend/xyz', {
    x: x !== '' ? parseFloat(x) : null,
    y: y !== '' ? parseFloat(y) : null,
  });
  if(!d.ok) appendLog([d.error || 'extend failed']);
}

async function doLoop(){
  const motors = [];
  if(document.getElementById('loopY').checked) motors.push('y');
  if(document.getElementById('loopX').checked) motors.push('x');
  if(!motors.length){ appendLog(['  select at least one motor']); return; }
  const cycles = parseInt(document.getElementById('loopN').value);
  const d = await post('/loop', {cycles, motors});
  if(!d.ok) appendLog([d.error || 'loop failed']);
}

async function doSetLimits(){
  const d = await post('/limits', {
    y: parseFloat(document.getElementById('limY').value),
    x: parseFloat(document.getElementById('limX').value),
    z_gripper: parseFloat(document.getElementById('limZGripper').value),
    z_pipettor: parseFloat(document.getElementById('limZPipettor').value),
  });
  if(!d.ok) appendLog([d.error || 'set limits failed']);
  else appendLog([`  limits set — y=${d.y}mm  x=${d.x}mm  z_gripper=${d.z_gripper}mm  z_pipettor=${d.z_pipettor}mm`]);
}

async function doTestAll(){
  const d = await post('/test_all');
  if(!d.ok) appendLog([d.error || 'test all failed']);
}

async function doStop(){
  await post('/stop');
}

let _manualMode = false;
async function doToggleManualMode(){
  const btn = document.getElementById('btnManualMode');
  const next = !_manualMode;
  btn.disabled = true;
  const d = await post('/manual_mode/' + (next ? 'enter' : 'exit'))
    .catch(e => ({ok:false, error:String(e)}));
  btn.disabled = false;
  if(!d.ok){ appendLog([d.error || 'manual mode toggle failed']); return; }
  setManualModeUi(next);
}

function setManualModeUi(active){
  _manualMode = active;
  const btn = document.getElementById('btnManualMode');
  const status = document.getElementById('manualModeStatus');
  btn.textContent = active ? 'exit manual mode' : 'enter manual mode';
  status.style.display = active ? '' : 'none';
  ['btnHome','btnExtend','btnLoop','btnTestAll','btnMoveToWell'].forEach(id => {
    const el = document.getElementById(id);
    if(el) el.disabled = active;
  });
}

async function doGripper(action){
  const d = await post('/gripper/' + action).catch(e => ({ok:false, error:String(e)}));
  const el = document.getElementById('gripStatus');
  if(!d.ok){
    el.textContent = d.error || 'failed';
    el.className = 'err';
  } else {
    el.textContent = action;
    el.className = action === 'open' ? 'ok' : 'warn';
  }
}

async function doGripperMove(){
  const pct = parseFloat(document.getElementById('gripPct').value);
  if(isNaN(pct) || pct < 0 || pct > 100){
    const el = document.getElementById('gripStatus');
    el.textContent = 'enter 0–100';
    el.className = 'err';
    return;
  }
  const d = await post('/gripper/moveto', {percent: pct}).catch(e => ({ok:false, error:String(e)}));
  const el = document.getElementById('gripStatus');
  if(!d.ok){
    el.textContent = d.error || 'failed';
    el.className = 'err';
  } else {
    el.textContent = pct + '%';
    el.className = 'ok';
  }
}

async function doTubegrip(action){
  const d = await post('/gripper/tubegrip/' + action).catch(e => ({ok:false, error:String(e)}));
  const el = document.getElementById('tubegripStatus');
  if(!d.ok){ el.textContent = d.error || 'failed'; el.className = 'err'; }
  else { el.textContent = action === 'open' ? 'open' : 'closed'; el.className = action === 'open' ? 'ok' : 'warn'; }
}

async function doTubegripMove(){
  const el = document.getElementById('tubegripStatus');
  const pct = parseFloat(document.getElementById('tubegripPct').value);
  if(isNaN(pct) || pct < 0 || pct > 100){ el.textContent = 'enter 0–100'; el.className = 'err'; return; }
  const d = await post('/gripper/tubegrip/moveto', {percent: pct}).catch(e => ({ok:false, error:String(e)}));
  if(!d.ok){ el.textContent = d.error || 'failed'; el.className = 'err'; }
  else { el.textContent = pct + '%'; el.className = 'ok'; }
}

let _tubegripOpenPct = 0, _tubegripClosePct = 100;
function _tubegripRenderTargets(){
  document.getElementById('tubegripTargets').textContent =
    `open button → ${_tubegripOpenPct}% · close button → ${_tubegripClosePct}%`;
}

async function doTubegripHome(){
  const el = document.getElementById('tubegripHomeResult');
  el.textContent = 'homing...'; el.className = '';
  const d = await post('/gripper/tubegrip/home').catch(e => ({ok:false, error:String(e)}));
  if(!d.ok){ el.textContent = d.error || 'failed'; el.className = 'err'; }
  else { el.textContent = `open=${d.open_peak}, close=${d.close_peak} pulses`; el.className = 'ok'; }
}

async function doTubegripSetOpen(){
  const el = document.getElementById('tubegripStatus');
  const pct = parseFloat(document.getElementById('tubegripPct').value);
  if(isNaN(pct) || pct < 0 || pct > 100){ el.textContent = 'enter 0–100'; el.className = 'err'; return; }
  const d = await post('/gripper/tubegrip/set_open_percent', {percent: pct}).catch(e => ({ok:false, error:String(e)}));
  if(!d.ok){ el.textContent = d.error || 'failed'; el.className = 'err'; return; }
  el.textContent = `open target set to ${d.open_percent}%`; el.className = 'ok';
  _tubegripOpenPct = d.open_percent;
  _tubegripRenderTargets();
}

async function doTubegripSetClose(){
  const el = document.getElementById('tubegripStatus');
  const pct = parseFloat(document.getElementById('tubegripPct').value);
  if(isNaN(pct) || pct < 0 || pct > 100){ el.textContent = 'enter 0–100'; el.className = 'err'; return; }
  const d = await post('/gripper/tubegrip/set_close_percent', {percent: pct}).catch(e => ({ok:false, error:String(e)}));
  if(!d.ok){ el.textContent = d.error || 'failed'; el.className = 'err'; return; }
  el.textContent = `close target set to ${d.close_percent}%`; el.className = 'ok';
  _tubegripClosePct = d.close_percent;
  _tubegripRenderTargets();
}

let _tubegripEnabled = true;
async function doTubegripToggleEnable(){
  const btn = document.getElementById('tubegripEnableBtn');
  const nextEnabled = !_tubegripEnabled;
  btn.disabled = true;
  const d = await post('/gripper/tubegrip/' + (nextEnabled ? 'enable' : 'disable'))
    .catch(e => ({ok:false, error:String(e)}));
  btn.disabled = false;
  if(!d.ok){
    const el = document.getElementById('tubegripStatus');
    el.textContent = d.error || 'failed'; el.className = 'err';
    return;
  }
  _tubegripEnabled = nextEnabled;
  btn.textContent = nextEnabled ? 'release TubeGrip (move by hand)' : 'TubeGrip released — click to hold torque';
}

async function doTubegripClearAlarm(){
  const el = document.getElementById('tubegripAlarm');
  const d = await post('/gripper/tubegrip/clear_alarm').catch(e => ({ok:false, error:String(e)}));
  if(!d.ok){ el.textContent = d.error || 'failed'; el.className = 'err'; }
  else {
    el.textContent = d.alarm + (d.alarm === 'normal' && !d.homed ? ' — press home before open/close' : '');
    el.className = d.alarm === 'normal' ? 'ok' : 'err';
  }
}

// Any Spin move re-enables holding torque on the driver (see _spin_move), so
// moves reset the release button back to its enabled state.
let _spinEnabled = true;
function setSpinEnabledUi(enabled){
  _spinEnabled = enabled;
  document.getElementById('spinEnableBtn').textContent =
    enabled ? 'release Spin (rotate by hand)' : 'Spin released — click to hold torque';
}

async function doSpinToggleEnable(){
  const btn = document.getElementById('spinEnableBtn');
  const nextEnabled = !_spinEnabled;
  btn.disabled = true;
  const d = await post('/gripper/spin/' + (nextEnabled ? 'enable' : 'disable'))
    .catch(e => ({ok:false, error:String(e)}));
  btn.disabled = false;
  const el = document.getElementById('spinStatus');
  if(!d.ok){ el.textContent = d.error || 'failed'; el.className = 'err'; return; }
  setSpinEnabledUi(nextEnabled);
  el.textContent = nextEnabled ? 'holding torque' : 'released — rotate by hand';
  el.className = nextEnabled ? 'ok' : 'warn';
}

async function doSpin(action){
  const d = await post('/gripper/spin/' + action).catch(e => ({ok:false, error:String(e)}));
  setSpinEnabledUi(true);
  const el = document.getElementById('spinStatus');
  if(!d.ok){ el.textContent = d.error || 'failed'; el.className = 'err'; }
  else { el.textContent = action === 'open' ? 'open' : 'closed'; el.className = action === 'open' ? 'ok' : 'warn'; }
}

async function doSpinMoveAngle(){
  const el = document.getElementById('spinStatus');
  const angle = parseFloat(document.getElementById('spinAngle').value);
  if(isNaN(angle)){ el.textContent = 'enter an angle'; el.className = 'err'; return; }
  const d = await post('/gripper/spin/moveto_angle', {angle_deg: angle}).catch(e => ({ok:false, error:String(e)}));
  setSpinEnabledUi(true);
  if(!d.ok){ el.textContent = d.error || 'failed'; el.className = 'err'; }
  else { el.textContent = angle + '°'; el.className = 'ok'; }
}

// Saves Spin's current encoder position (read from the driver) as the
// open/close/cap holder position in one click — e.g. release Spin, twist
// it by hand to the spot, then set current.
async function doSpinSetCurrent(which, inputId){
  const el = document.getElementById('spinStatus');
  const d = await post('/gripper/spin/set_current/' + which).catch(e => ({ok:false, error:String(e)}));
  if(!d.ok){ el.textContent = d.error || 'failed'; el.className = 'err'; return; }
  document.getElementById(inputId).value = d.pos;
  el.textContent = `${which.replace('_', ' ')} saved at ${d.pos} pulses`; el.className = 'ok';
}

async function doSpinSetPositions(){
  const el = document.getElementById('spinStatus');
  const openPos = parseInt(document.getElementById('spinOpenPos').value, 10);
  const closePos = parseInt(document.getElementById('spinClosePos').value, 10);
  const capHolderPos = parseInt(document.getElementById('spinCapHolderPos').value, 10);
  if(isNaN(openPos) || isNaN(closePos) || isNaN(capHolderPos)){ el.textContent = 'enter open/close/cap holder pulses'; el.className = 'err'; return; }
  const d = await post('/gripper/spin/positions', {open_pos: openPos, close_pos: closePos, cap_holder_pos: capHolderPos})
    .catch(e => ({ok:false, error:String(e)}));
  if(!d.ok){ el.textContent = d.error || 'failed'; el.className = 'err'; }
  else { el.textContent = `positions saved (open=${openPos}, close=${closePos}, cap holder=${capHolderPos} pulses)`; el.className = 'ok'; }
}

// from/to are {plate, well} so tubes can be moved between different plates
let _fromWell = null;
let _toWell   = null;

function setFromWell() {
  if (!_selectedWell) { appendLog(['select a well first']); return; }
  _fromWell = { plate: _currentPlate, well: _selectedWell };
  const pos = getWellPosition(_fromWell.well, plateData(_fromWell.plate));
  document.getElementById('fromWell').textContent = `${_fromWell.plate} / ${_fromWell.well}`;
  document.getElementById('fromPos').textContent  = pos ? `x=${pos.x}  y=${pos.y}` : '';
}

function setToWell() {
  if (!_selectedWell) { appendLog(['select a well first']); return; }
  _toWell = { plate: _currentPlate, well: _selectedWell };
  const pos = getWellPosition(_toWell.well, plateData(_toWell.plate));
  document.getElementById('toWell').textContent = `${_toWell.plate} / ${_toWell.well}`;
  document.getElementById('toPos').textContent  = pos ? `x=${pos.x}  y=${pos.y}` : '';
}

// Move the tracked tube from source to destination plate (may be the same
// plate). Updates each affected plate's tube set and persists them, then
// re-syncs the on-screen grid if it shows an affected plate.
async function moveTubeTracking(from, to) {
  const src = _plates[from.plate] = _plates[from.plate] || {};
  const srcTubes = new Set(src.tubes || []);
  srcTubes.delete(from.well);
  src.tubes = Array.from(srcTubes);

  const dst = _plates[to.plate] = _plates[to.plate] || {};
  const dstTubes = new Set(dst.tubes || []);   // re-read: same object if same plate
  dstTubes.add(to.well);
  dst.tubes = Array.from(dstTubes);

  if (_currentPlate === from.plate || _currentPlate === to.plate) {
    _tubeMap = new Set((_plates[_currentPlate] || {}).tubes || []);
    refreshWellClasses();
  }

  for (const p of new Set([from.plate, to.plate])) {
    await post('/calibration/save_tubes', {plate: p, tubes: (_plates[p] || {}).tubes || []})
      .catch(() => {});
  }
}

async function doMoveTube() {
  if (!_fromWell || !_toWell) { appendLog(['set from and to wells first']); return; }
  const fromPos = getWellPosition(_fromWell.well, plateData(_fromWell.plate));
  const toPos   = getWellPosition(_toWell.well,   plateData(_toWell.plate));
  if (!fromPos || !toPos) { appendLog(['calibrate both plates first (all 4 corners, or one corner with pitch set)']); return; }
  const grabMm  = parseFloat(document.getElementById('grabZ').value);
  const placeMm = parseFloat(document.getElementById('placeZ').value);
  if (isNaN(grabMm) || isNaN(placeMm)) {
    appendLog(['set grab and place z heights in the Control tab first']); return;
  }
  const d = await post('/move_tube', {
    from_x: fromPos.x, from_y: fromPos.y,
    to_x:   toPos.x,   to_y:   toPos.y,
    grab_z_mm: grabMm, place_z_mm: placeMm,
  }).catch(e => ({ok:false, error:String(e)}));
  if (!d.ok) { appendLog([d.error || 'move tube failed']); return; }
  // update tube tracking: remove from source plate, add to destination plate
  await moveTubeTracking(_fromWell, _toWell);
}

async function doTestPosition(){
  if (!_selectedWell) { appendLog(['select a well first']); return; }
  const pos = getWellPosition(_selectedWell);
  if (!pos) { appendLog(['calibrate all 4 corners, or one corner with pitch set']); return; }
  const grabMm  = parseFloat(document.getElementById('grabZ').value);
  const placeMm = parseFloat(document.getElementById('placeZ').value);
  if (isNaN(grabMm) || isNaN(placeMm)) {
    appendLog(['set grab and place z heights in the Control tab first']); return;
  }
  const d = await post('/test_well', {
    x: pos.x, y: pos.y,
    grab_z_mm: grabMm, place_z_mm: placeMm,
  }).catch(e => ({ok:false, error:String(e)}));
  if (!d.ok) appendLog([d.error || 'test failed']);
}

async function doMoveToWell(){
  if (!_selectedWell) { appendLog(['select a well first']); return; }
  const pos = getWellPosition(_selectedWell);
  if (!pos) { appendLog(['calibrate all 4 corners, or one corner with pitch set']); return; }
  await post('/gripper/open').catch(() => {});
  const d = await post('/extend/xyz', {x: pos.x, y: pos.y})
    .catch(e => ({ok:false, error:String(e)}));
  if (!d.ok) { appendLog([d.error || 'move failed']); return; }
  await waitUntilIdle();
  const dz = await post('/extend', {motor: 'z_gripper', mm: pos.z})
    .catch(e => ({ok:false, error:String(e)}));
  if (!dz.ok) appendLog([dz.error || 'z move failed']);
}

async function doDecapTube(){
  if (!_selectedWell) { appendLog(['select a well first']); return; }
  const d = await post('/decap_tube', {plate: _currentPlate, well: _selectedWell})
    .catch(e => ({ok:false, error:String(e)}));
  if (!d.ok) appendLog([d.error || 'decap tube failed']);
}

async function doGrab(){
  const mm = parseFloat(document.getElementById('grabZ').value);
  if(isNaN(mm)){ appendLog(['enter grab z height']); return; }
  const d = await post('/grab', {z_mm: mm}).catch(e => ({ok:false, error:String(e)}));
  if(!d.ok) appendLog([d.error || 'grab failed']);
}

async function doPlace(){
  const mm = parseFloat(document.getElementById('placeZ').value);
  if(isNaN(mm)){ appendLog(['enter place z height']); return; }
  const d = await post('/place', {z_mm: mm}).catch(e => ({ok:false, error:String(e)}));
  if(!d.ok) appendLog([d.error || 'place failed']);
}

async function doDecap(){
  const mm = parseFloat(document.getElementById('decapZ').value);
  if(isNaN(mm)){ appendLog(['enter decap z height']); return; }
  const d = await post('/decap', {z_mm: mm}).catch(e => ({ok:false, error:String(e)}));
  if(!d.ok) appendLog([d.error || 'decap failed']);
}

async function doRecap(){
  const mm = parseFloat(document.getElementById('recapZ').value);
  if(isNaN(mm)){ appendLog(['enter recap z height']); return; }
  const d = await post('/recap', {z_mm: mm}).catch(e => ({ok:false, error:String(e)}));
  if(!d.ok) appendLog([d.error || 'recap failed']);
}

async function doReadPositions(){
  const r = await fetch('/positions');
  const d = await r.json();
  if(!d.ok){ appendLog([d.error || 'read failed']); return; }
  const pos = d.positions || {};
  for(const [name, info] of Object.entries(pos)){
    if(info.error){
      appendLog([`  [${name}] read error`]);
    } else {
      appendLog([`  [${name}]  ${info.pulses} pulses  ${info.val} ${info.unit}`]);
    }
  }
}

function updateState(d){
  if (d.laser !== undefined) updateLaserLive(d.laser);
  const busy = d.busy;
  document.getElementById('btnStop').disabled = !busy;
  document.querySelectorAll('.loop-stop').forEach(b => b.disabled = !busy);
  const bs = document.getElementById('busyStatus');
  bs.textContent = busy ? 'BUSY' : '';
  bs.className   = busy ? 'warn' : '';

  if(d.showcase_phase) setShowcaseUi(d.showcase_phase, d.showcase_loop, d.showcase_loop_last);

  if(d.manual_mode !== undefined && d.manual_mode !== _manualMode){
    setManualModeUi(d.manual_mode);
  }

  const homed = d.homed || {};
  const pos   = d.positions || {};
  for(const name of ['y','x','z_gripper','z_pipettor']){
    const pEl = document.getElementById('pos_' + name);
    const hEl = document.getElementById('homed_' + name);
    if(pEl){
      const info = pos[name];
      pEl.textContent = (info && info.val !== null && info.val !== undefined) ? info.val : '—';
    }
    if(hEl){
      const h = homed[name];
      hEl.textContent = h ? 'yes' : 'no';
      hEl.className   = h ? 'ok' : '';
    }
  }

  if(d.spin){
    const spEl = document.getElementById('spinPosition');
    if(spEl) spEl.textContent = d.spin.position ?? '—';
    const spBtn = document.getElementById('spinEnableBtn');
    if(spBtn && !spBtn.disabled && typeof d.spin.enabled === 'boolean') setSpinEnabledUi(d.spin.enabled);
  }

  if(d.holder_plates){
    // Live-sync cap occupancy for every known plate (not just the one
    // currently shown) so switching tabs never shows stale data, same
    // idea as the tube shuffle loop's plates_tubes push.
    Object.keys(d.holder_plates).forEach(name => {
      _capPlates[name] = _capPlates[name] || {};
      _capPlates[name].caps = d.holder_plates[name].caps || [];
    });
    if (_currentCapPlate && d.holder_plates[_currentCapPlate]) {
      const newCaps = JSON.stringify((d.holder_plates[_currentCapPlate].caps || []).slice().sort());
      const curCaps = JSON.stringify(Array.from(_capMap).sort());
      if (newCaps !== curCaps) {
        _capMap = new Set(d.holder_plates[_currentCapPlate].caps || []);
        refreshCapClasses();
      }
    }
  }
}

let _wsActive = false;

function startWs(){
  stopWs();
  _wsActive = true;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws');
  ws.onmessage = e => {
    const d = JSON.parse(e.data);
    if(d.log && d.log.length) appendLog(d.log);
    updateState(d);
  };
  ws.onclose = () => {
    ws = null;
    if(_wsActive) setTimeout(startWs, 1000);
  };
}

function stopWs(){
  _wsActive = false;
  if(ws){ ws.close(); ws = null; }
}

// Init — restore state on page load/refresh
loadRecentLogins();
loadErrors();
loadNotes();
fetch('/state').then(r=>r.json()).then(d=>{
  if(d.user){
    document.getElementById('loginSection').style.display    = 'none';
    document.getElementById('connSection').style.display     = '';
    document.getElementById('sessSection').style.display     = '';
    document.getElementById('notePostSection').style.display = '';
    document.getElementById('currentUser').textContent   = d.user;
    document.getElementById('currentMachine').textContent = d.machine || '';
    loadSessions();
  }
  if(d.limits){
    // The limit inputs' HTML value="..." attributes are just placeholders
    // for before this fetch resolves — the real, currently-effective (and
    // possibly persisted-from-a-restart) limit always overrides them here.
    if(d.limits.y !== undefined) document.getElementById('limY').value = d.limits.y;
    if(d.limits.x !== undefined) document.getElementById('limX').value = d.limits.x;
    if(d.limits.z_gripper !== undefined) document.getElementById('limZGripper').value = d.limits.z_gripper;
    if(d.limits.z_pipettor !== undefined) document.getElementById('limZPipettor').value = d.limits.z_pipettor;
  }
  if(d.spin){
    document.getElementById('spinOpenPos').value = d.spin.open_pos;
    document.getElementById('spinClosePos').value = d.spin.close_pos;
    document.getElementById('spinCapHolderPos').value = d.spin.cap_holder_pos;
    document.getElementById('spinPosition').textContent = d.spin.position ?? '—';
  }
  if(d.tubegrip){
    _tubegripOpenPct = d.tubegrip.open_percent;
    _tubegripClosePct = d.tubegrip.close_percent;
    _tubegripRenderTargets();
  }
  if(d.connected){
    setConnStatus('connected', 'ok');
    document.getElementById('btnConn').disabled = true;
    document.getElementById('btnDisc').disabled = false;
    ['statusSection','ctrlSection','gripSection','grabPlaceSection','logSection'].forEach(
      id => document.getElementById(id).style.display = '');
    startWs();
  }
}).catch(()=>{});

window.addEventListener('beforeunload', () => { navigator.sendBeacon('/logout'); });
</script>
</body>
</html>"""


# ── Port watcher ──────────────────────────────────────────────────────────────
# Power-cycling or replugging a USB adapter recreates its device node, often
# under a different number (ttyUSB0 -> ttyUSB1), and the old open handle
# goes dead. Every PORT_WATCH_INTERVAL_S this notices a vanished/recreated
# node for each auto-discovered device, releases the dead handle, and —
# whenever the set of USB serial ports changes — searches again and
# reconnects on the new port. Ports typed in by hand are left alone.
_bus_lost = False
_pipettor_lost: Optional[dict] = None   # connect settings to restore once the pipettor is found again

def _port_watch_tick(ports_changed: bool) -> None:
    global _client, _bus_lost, _gripper_client, _gripper_port, _pipettor_device, _pipettor_info, _pipettor_lost

    # motor bus
    if _connected and _bus_auto:
        if not _bus_lost and _port_gone(_bus_port, _bus_dev):
            _log(f"[bus] {_bus_port} went away (power cycle / unplug) — will reconnect when it's back")
            with _lock:
                if _client:
                    _client.close()
                _client = None
            _bus_lost = True
            _forget_homing("motor bus lost")
        if _bus_lost and ports_changed:
            port = _find_motor_bus(_bus_baud, skip=({_gripper_port} | _ports_to_skip()) - {None},
                                   quiet=True)
            if port:
                with _lock:
                    ok = _do_connect(port, _bus_baud)
                if ok:
                    _bus_lost = False
                    _spin_set_enabled(True)
                    _log(f"[bus] reconnected on {port}")

    # static gripper
    if _is_auto(GRIPPER["port"]):
        with _gripper_lock:
            if _gripper_port is not None and _port_gone(_gripper_port, _gripper_dev):
                _log(f"  [gripper] {_gripper_port} went away (power cycle / unplug) — will reconnect when it's back")
                if _gripper_client is not None:
                    _gripper_client.close()
                _gripper_client = None
                _gripper_port = None
            if _connected and _gripper_port is None and ports_changed:
                _gripper_connect(quiet=True)

    # pipettor
    if _pipettor_info.get("connected") and _pipettor_info.get("auto") \
            and _port_gone(_pipettor_info.get("port"), _pipettor_dev):
        _log(f"[pipettor] {_pipettor_info['port']} went away (power cycle / unplug) — will reconnect when it's back")
        with _pipettor_lock:
            if _pipettor_device is not None:
                _pipettor_device.transport.close()
            _pipettor_device = None
        _pipettor_lost = dict(_pipettor_info)
        _pipettor_info = {**_pipettor_info, "connected": False, "port": None}
    if _pipettor_lost and ports_changed:
        info = _pipettor_lost
        pipettor_connect(PipettorConnectReq(port="auto", baud=info["baud"], address=info["address"],
                                                head=info.get("head", "oem")))
        if _pipettor_info.get("connected"):
            _log(f"[pipettor] reconnected on {_pipettor_info['port']}")
        else:
            _pipettor_lost = info   # pipettor_connect clears it; keep waiting

def _port_watch_loop() -> None:
    last = None
    while True:
        time.sleep(PORT_WATCH_INTERVAL_S)
        try:
            now = (tuple(_serial_candidates()), tuple(sorted(_ports_held_elsewhere())))
            _port_watch_tick(ports_changed=(now != last))
            last = now
        except Exception as e:
            _log(f"[ports] watcher error: {e}")

threading.Thread(target=_port_watch_loop, daemon=True, name="port-watch").start()


if __name__ == "__main__":
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "localhost"
    print(f"\n{'='*52}")
    print(f"  Paul Cherry Picker")
    print(f"{'='*52}")
    print(f"  Local:   http://localhost:8767")
    print(f"  Network: http://{ip}:8767")
    print(f"{'='*52}\n")
    uvicorn.run(app, host="0.0.0.0", port=8767, log_level="warning")
