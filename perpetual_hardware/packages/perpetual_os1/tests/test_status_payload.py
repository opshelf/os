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

try:
    import nats as _nats_available  # noqa: F401
except Exception:
    nats_module = types.ModuleType("nats")
    nats_aio = types.ModuleType("nats.aio")
    nats_aio_client = types.SimpleNamespace(Client=object)
    nats_aio_msg = types.SimpleNamespace(Msg=object)
    nats_errors = types.SimpleNamespace(TimeoutError=Exception)
    nats_js = types.ModuleType("nats.js")
    nats_js_errors = types.SimpleNamespace(NotFoundError=Exception)
    nats_js_client = types.SimpleNamespace(JetStreamContext=object)

    nats_module.aio = nats_aio
    nats_module.errors = nats_errors
    nats_module.js = nats_js
    nats_aio.client = nats_aio_client
    nats_aio.msg = nats_aio_msg
    nats_js.errors = nats_js_errors
    nats_js.client = nats_js_client

    sys.modules.setdefault("nats", nats_module)
    sys.modules.setdefault("nats.aio", nats_aio)
    sys.modules.setdefault("nats.aio.client", nats_aio_client)
    sys.modules.setdefault("nats.aio.msg", nats_aio_msg)
    sys.modules.setdefault("nats.errors", nats_errors)
    sys.modules.setdefault("nats.js", nats_js)
    sys.modules.setdefault("nats.js.errors", nats_js_errors)
    sys.modules.setdefault("nats.js.client", nats_js_client)

from perpetual_os1.robot_worker import _transform_worker_status_payload


def test_transform_worker_payload_includes_needs_home() -> None:
    payload = {
        "robot_status": {
            "storage": {"left_module": [], "right_module": []},
            "state": {"current_cell": None},
        },
        "error_queue": [],
        "needs_home": True,
        "needs_home_reason": "Axis stall",
    }

    result = _transform_worker_status_payload(payload)

    assert result["needs_home"] is True
    assert result["needs_home_reason"] == "Axis stall"
