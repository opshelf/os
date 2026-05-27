"""Robot status payload transformation tests."""
# ruff: noqa: E402

import pathlib
import sys
import types

BASE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(BASE.parents[1] / "src"))
sys.path.insert(0, str(BASE.parents[3] / "packages" / "hardware" / "src"))

try:
    import sentry_sdk as _sentry_available  # noqa: F401
except Exception:
    sys.modules.setdefault(
        "sentry_sdk",
        types.SimpleNamespace(
            init=lambda **kwargs: None,
            capture_exception=lambda *args, **kwargs: None,
            capture_message=lambda *args, **kwargs: None,
            push_scope=lambda: types.SimpleNamespace(
                __enter__=lambda s: types.SimpleNamespace(
                    set_tag=lambda *a, **k: None,
                    set_context=lambda *a, **k: None,
                    set_extra=lambda *a, **k: None,
                ),
                __exit__=lambda *a, **k: None,
            ),
            flush=lambda **kwargs: None,
        ),
    )

try:
    import pymodbus as _pymodbus_available  # noqa: F401
except Exception:
    class _DummyAsyncModbusSerialClient:
        def __init__(self, *_, **__):
            self.connected = False

        async def connect(self, *_, **__):
            return False

        def close(self):
            self.connected = False


    pymodbus_stub = types.SimpleNamespace(
        FramerType=types.SimpleNamespace(RTU=None),
        client=types.SimpleNamespace(AsyncModbusSerialClient=_DummyAsyncModbusSerialClient),
        exceptions=types.SimpleNamespace(
            ModbusIOException=Exception,
            ModbusException=Exception,
        ),
    )
    sys.modules.setdefault("pymodbus", pymodbus_stub)
    sys.modules.setdefault("pymodbus.client", pymodbus_stub.client)
    sys.modules.setdefault("pymodbus.exceptions", pymodbus_stub.exceptions)

try:
    import cv2 as _cv2_available  # noqa: F401
except Exception:
    sys.modules.setdefault("cv2", types.SimpleNamespace())

from openshelf_os.robot_worker import _transform_worker_status_payload


def test_transform_worker_payload_includes_needs_home() -> None:
    payload = {
        "robot_status": {
            "storage": {"left_module": [], "right_module": []},
            "state": {"current_cell": None},
        },
        "error_queue": [],
        "needs_home": True,
        "needs_home_reason": "Axis stall",
        "needs_home_axes": ["z_axis", "stage"],
    }

    result = _transform_worker_status_payload(payload)

    assert result["needs_home"] is True
    assert result["needs_home_reason"] == "Axis stall"
    assert result["needs_home_axes"] == ["z_axis", "stage"]


def test_transform_worker_payload_preserves_axis_telemetry() -> None:
    payload = {
        "robot_status": {
            "gantry": {
                "x_axis": {"position": 12.5, "homed": 1, "hard_stopped": 0},
                "y_axis": {"position": 7.0, "homed": 0, "hard_stopped": 1},
            },
            "tool": {
                "z_axis": {"position": 3.25, "homed": 1, "hard_stopped": 0},
            },
            "storage": {"left_module": [], "right_module": []},
            "state": {"current_cell": None},
        },
        "error_queue": [],
    }

    result = _transform_worker_status_payload(payload)

    assert result["robot_status"]["gantry"]["x_axis"]["position"] == 12.5
    assert result["robot_status"]["gantry"]["x_axis"]["homed"] == 1
    assert result["robot_status"]["gantry"]["y_axis"]["hard_stopped"] == 1
    assert result["robot_status"]["tool"]["z_axis"]["position"] == 3.25
