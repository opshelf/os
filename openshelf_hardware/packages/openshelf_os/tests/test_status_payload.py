"""Robot status payload transformation tests."""
# ruff: noqa: E402

import asyncio
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
            new_scope=lambda: types.SimpleNamespace(
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

from openshelf_os.robot.worker import RobotWorker, _transform_worker_status_payload


def test_transform_worker_payload_includes_needs_home() -> None:
    payload = {
        "robot_status": {
            "storage": {"front_module": [], "back_module": []},
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


def test_transform_worker_payload_preserves_io_door_state() -> None:
    payload = {
        "robot_status": {
            "gantry": {},
            "tool": {},
            "state": {},
            "storage": {},
            "io_door_open": True,
        }
    }

    result = _transform_worker_status_payload(payload)

    assert result["robot_status"]["io_door_open"] is True


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
            "storage": {"front_module": [], "back_module": []},
            "state": {"current_cell": None},
        },
        "error_queue": [],
    }

    result = _transform_worker_status_payload(payload)

    assert result["robot_status"]["gantry"]["x_axis"]["position"] == 12.5
    assert result["robot_status"]["gantry"]["x_axis"]["homed"] == 1
    assert result["robot_status"]["gantry"]["y_axis"]["hard_stopped"] == 1
    assert result["robot_status"]["tool"]["z_axis"]["position"] == 3.25


def test_transform_worker_payload_preserves_motor_status_fields() -> None:
    payload = {
        "robot_status": {
            "motors": {
                "x_axis": {
                    "position": 12.5,
                    "homed": 1,
                    "hard_stopped": 0,
                    "status": "moving",
                    "current_limit": 3.6,
                    "max_following_error": 4000,
                },
                "y_axis": {
                    "position": 7.0,
                    "homed": 0,
                    "hard_stopped": 1,
                    "status": "fault",
                    "current_limit": None,
                    "max_following_error": None,
                },
            },
            "storage": {"front_module": [], "back_module": []},
            "state": {"current_cell": None},
        },
        "error_queue": [],
    }

    result = _transform_worker_status_payload(payload)
    x_axis = result["robot_status"]["motors"]["x_axis"]
    y_axis = result["robot_status"]["motors"]["y_axis"]

    assert x_axis["status"] == "moving"
    assert x_axis["current_limit"] == 3.6
    assert x_axis["max_following_error"] == 4000
    assert y_axis["status"] == "fault"
    assert y_axis["current_limit"] is None
    assert y_axis["max_following_error"] is None


def test_transform_worker_payload_defaults_missing_motor_status_fields() -> None:
    payload = {
        "robot_status": {
            "motors": {"x_axis": {"position": 12.5, "homed": 1, "hard_stopped": 0}},
            "storage": {"front_module": [], "back_module": []},
            "state": {"current_cell": None},
        },
        "error_queue": [],
    }

    result = _transform_worker_status_payload(payload)
    x_axis = result["robot_status"]["motors"]["x_axis"]

    assert x_axis["status"] == "idle"
    assert x_axis["current_limit"] is None
    assert x_axis["max_following_error"] is None


def test_transform_worker_payload_preserves_safety_status() -> None:
    payload = {
        "robot_status": {
            "storage": {"front_module": [], "back_module": []},
            "state": {"current_cell": None},
            "cabinet_doors": {
                "top_closed": False,
                "bottom_closed": True,
                "closed": False,
                "top_open": True,
                "bottom_open": False,
                "open": True,
            },
            "safety": {
                "active": True,
                "motion_blocked": True,
                "awaiting_ack": True,
                "can_acknowledge": False,
                "active_sources": ["cabinet_doors"],
                "sources": {"cabinet_doors": {"active": True}},
                "interruption_id": 1,
                "last_changed_at": "2026-06-01T00:00:00+00:00",
            },
        },
        "error_queue": [],
    }

    result = _transform_worker_status_payload(payload)

    assert result["robot_status"]["cabinet_doors"]["open"] is True
    assert result["robot_status"]["safety"]["active_sources"] == ["cabinet_doors"]


def test_transform_worker_payload_preserves_scale_status() -> None:
    payload = {
        "robot_status": {
            "storage": {"front_module": [], "back_module": []},
            "state": {"current_cell": None},
            "scale": {
                "available": True,
                "weight_grams": 12.5,
                "stable": True,
                "valid": True,
                "occupied": True,
                "threshold_grams": 5.0,
            },
        },
        "error_queue": [],
    }

    result = _transform_worker_status_payload(payload)

    assert result["robot_status"]["scale"]["occupied"] is True
    assert result["robot_status"]["scale"]["weight_grams"] == 12.5


def test_scale_refresh_caches_stable_weight() -> None:
    async def run() -> None:
        async def get_scale_data() -> dict[str, object]:
            return {
                "weight": -12.5,
                "decimal_position": 1,
                "status": {"stable": True, "valid": True, "overload": False, "zero": False},
            }

        worker = RobotWorker.__new__(RobotWorker)
        worker._robot = types.SimpleNamespace(get_scale_data=get_scale_data)
        worker._scale_lock = asyncio.Lock()
        worker._latest_scale_status = None
        worker._latest_scale_status_monotonic = None
        worker._last_published_scale_key = None

        should_publish = await worker._refresh_scale_status()

        assert should_publish is True
        assert worker._latest_scale_status is not None
        assert worker._latest_scale_status.weight_grams == 12.5
        assert worker._latest_scale_status.occupied is True
        assert worker.get_cached_scale_status() is worker._latest_scale_status

    asyncio.run(run())


def test_scale_refresh_caches_stable_weight_when_valid_flag_is_false() -> None:
    async def run() -> None:
        async def get_scale_data() -> dict[str, object]:
            return {
                "weight": 210.0,
                "decimal_position": 1,
                "status": {"stable": True, "valid": False, "overload": False, "zero": False},
            }

        worker = RobotWorker.__new__(RobotWorker)
        worker._robot = types.SimpleNamespace(get_scale_data=get_scale_data)
        worker._scale_lock = asyncio.Lock()
        worker._latest_scale_status = None
        worker._latest_scale_status_monotonic = None
        worker._last_published_scale_key = None

        should_publish = await worker._refresh_scale_status()

        assert should_publish is True
        assert worker._latest_scale_status is not None
        assert worker._latest_scale_status.valid is False
        assert worker._latest_scale_status.weight_grams == 210.0
        assert worker._latest_scale_status.occupied is True
        assert worker.get_cached_scale_status() is worker._latest_scale_status

    asyncio.run(run())


def test_successful_scale_calibration_mutations_invalidate_cached_status() -> None:
    async def run() -> None:
        async def set_params(**_: object) -> bool:
            return True

        async def calibrate_zero(**_: object) -> dict[str, bool]:
            return {"success": True}

        async def calibrate_weight(**_: object) -> dict[str, bool]:
            return {"success": True}

        async def tare() -> bool:
            return True

        worker = RobotWorker.__new__(RobotWorker)
        worker._robot = types.SimpleNamespace(
            set_scale_calibration_params=set_params,
            calibrate_scale_zero=calibrate_zero,
            calibrate_scale_weight=calibrate_weight,
            scale_tare=tare,
        )
        worker._scale_lock = asyncio.Lock()

        mutations = (
            lambda: worker.set_scale_calibration_params(6000, 500),
            worker.calibrate_scale_zero,
            worker.calibrate_scale_weight,
            worker.scale_tare,
        )
        for mutate in mutations:
            worker._latest_scale_status = object()
            worker._latest_scale_status_monotonic = 1.0
            await mutate()
            assert worker._latest_scale_status is None
            assert worker._latest_scale_status_monotonic is None

    asyncio.run(run())
