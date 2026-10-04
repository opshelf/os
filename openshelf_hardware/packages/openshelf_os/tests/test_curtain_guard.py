"""Curtain guard unit tests with lightweight dependency stubs."""
# ruff: noqa: E402

import asyncio
import pathlib
import sys
import unittest
import types

# Ensure workspace src paths are importable when running tests directly.
BASE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(BASE.parents[1] / "src"))
sys.path.insert(0, str(BASE.parents[3] / "packages" / "hardware" / "src"))

# Stub sentry_sdk to avoid optional dependency requirement during unit tests if absent.
try:
    import sentry_sdk as _sentry_available  # noqa: F401
except Exception:
    sys.modules.setdefault("sentry_sdk", types.SimpleNamespace(init=lambda **kwargs: None))

# Only stub pydantic if it is not available in the environment.
try:
    import pydantic as _pydantic_available  # noqa: F401
except Exception:
    class _DummyBaseModel:
        def __init__(self, *args, **kwargs):
            pass

        @classmethod
        def model_validate(cls, value):
            return value

        def model_dump(self, *args, **kwargs):
            return {}


    def _identity_decorator(*args, **kwargs):
        return lambda fn: fn


    class _DummyRootModel(list):
        def __init__(self, root=None):
            if root is None:
                root = []
            super().__init__(root)
            self.root = root

        @classmethod
        def model_validate(cls, value):
            return cls(value)


    class _DummyTypeAdapter:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, model):
            self.model = model

        def validate_json(self, data):
            return data


    pydantic_stub = types.SimpleNamespace(
        BaseModel=_DummyBaseModel,
        RootModel=_DummyRootModel,
        Field=lambda *args, **kwargs: kwargs.get("default", None),
        ValidationError=Exception,
        field_validator=_identity_decorator,
        model_validator=_identity_decorator,
        TypeAdapter=_DummyTypeAdapter,
    )
    sys.modules.setdefault("pydantic", pydantic_stub)

# Stub pymodbus to avoid dependency during imports if absent.
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
            ModbusIOException=Exception, ModbusException=Exception
        ),
    )
    sys.modules.setdefault("pymodbus", pymodbus_stub)
    sys.modules.setdefault("pymodbus.client", pymodbus_stub.client)
    sys.modules.setdefault("pymodbus.exceptions", pymodbus_stub.exceptions)

# Stub cv2 used by hardware modules if absent.
try:
    import cv2 as _cv2_available  # noqa: F401
except Exception:
    sys.modules.setdefault("cv2", types.SimpleNamespace())

# Stub serial_asyncio used by barcode protocol.
try:
    import serial_asyncio as _serial_asyncio_available  # noqa: F401
except Exception:
    sys.modules.setdefault("serial_asyncio", types.SimpleNamespace())
# Stub serial tools.
try:
    import serial as _serial_available  # noqa: F401
except Exception:
    serial_tools = types.ModuleType("serial.tools")
    serial_tools.list_ports = types.SimpleNamespace(comports=lambda: [])
    serial_module = types.ModuleType("serial")
    serial_module.tools = serial_tools
    sys.modules.setdefault("serial", serial_module)
    sys.modules.setdefault("serial.tools", serial_tools)
    sys.modules.setdefault("serial.tools.list_ports", serial_tools.list_ports)

from openshelf_os.robot.worker import RobotWorker


class FakeRobot:
    def __init__(self) -> None:
        self._curtain_state = False
        self._door_state = False
        self._cabinet_door_state = {
            "top_closed": True,
            "bottom_closed": True,
            "closed": True,
            "top_open": False,
            "bottom_open": False,
            "open": False,
        }
        self.pause_calls = 0
        self.resume_calls = 0
        self._operation_context = None
        self.safety_guard_bypassed = False
        self.light_curtain_bypassed = False

    def set_curtain_state(self, state):
        self._curtain_state = state

    def set_door_state(self, state):
        self._door_state = state

    def set_cabinet_door_state(self, state):
        self._cabinet_door_state = state

    def set_safety_guard_bypassed(self, enabled):
        self.safety_guard_bypassed = enabled

    def set_light_curtain_bypassed(self, enabled):
        self.light_curtain_bypassed = enabled

    def set_operation_context(self, context):
        self._operation_context = context

    def get_curtain_state(self):
        return self._curtain_state

    async def refresh_door_state(self):
        return self._door_state

    def get_cabinet_door_state(self):
        return self._cabinet_door_state

    def get_operation_context(self):
        return self._operation_context

    def block_motion_for_safety(self):
        self.pause_calls += 1

    def release_motion_safety(self):
        self.resume_calls += 1


class CurtainGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.robot = FakeRobot()
        self.worker = RobotWorker(self.robot)

    def test_light_curtain_requires_open_io_door(self):
        self.robot.set_curtain_state(True)
        self.robot.set_door_state(False)

        asyncio.run(self.worker._evaluate_safety_guard())

        self.assertFalse(self.worker.get_safety_status()["active"])
        self.assertEqual(self.robot.pause_calls, 0)

    def test_light_curtain_blocks_when_io_door_is_opening(self):
        self.robot.set_curtain_state(True)
        self.robot.set_door_state(False)
        self.robot.set_operation_context({"phase": "io_place.open_door"})

        asyncio.run(self.worker._evaluate_safety_guard())

        safety = self.worker.get_safety_status()
        self.assertTrue(safety["active"])
        self.assertEqual(safety["active_sources"], ["light_curtain"])
        self.assertTrue(safety["sources"]["light_curtain"]["io_door_opening"])
        self.assertTrue(safety["awaiting_ack"])
        self.assertEqual(self.robot.pause_calls, 1)

    def test_guard_blocks_when_curtain_and_door_open(self):
        self.robot.set_curtain_state(True)
        self.robot.set_door_state(True)

        asyncio.run(self.worker._evaluate_safety_guard())

        safety = self.worker.get_safety_status()
        self.assertTrue(safety["active"])
        self.assertEqual(safety["active_sources"], ["light_curtain"])
        self.assertTrue(safety["awaiting_ack"])
        self.assertEqual(self.robot.pause_calls, 1)
        self.assertEqual(self.robot.resume_calls, 0)

    def test_light_curtain_does_not_block_when_crashed(self):
        self.worker.set_crash_state("test crash")
        self.robot.set_curtain_state(True)
        self.robot.set_door_state(True)

        asyncio.run(self.worker._evaluate_safety_guard())

        safety = self.worker.get_safety_status()
        self.assertFalse(safety["active"])
        self.assertFalse(safety["sources"]["light_curtain"]["active"])
        self.assertEqual(self.robot.pause_calls, 0)

    def test_light_curtain_does_not_block_when_emergency_stopped(self):
        self.worker.set_emergency_stop_state("test emergency stop")
        self.robot.set_curtain_state(True)
        self.robot.set_door_state(True)

        asyncio.run(self.worker._evaluate_safety_guard())

        safety = self.worker.get_safety_status()
        self.assertFalse(safety["active"])
        self.assertFalse(safety["sources"]["light_curtain"]["active"])
        self.assertEqual(self.robot.pause_calls, 0)

    def _assert_curtain_change_ignored(self, set_state, reason):
        evaluate_calls = 0
        publish_calls = 0

        async def did_evaluate_safety_guard():
            nonlocal evaluate_calls
            evaluate_calls += 1
            return True

        async def did_publish_status():
            nonlocal publish_calls
            publish_calls += 1

        set_state(reason)
        self.worker._evaluate_safety_guard = did_evaluate_safety_guard
        self.worker._publish_status = did_publish_status

        async def run_test():
            self.worker._on_light_curtain_change(True)
            await asyncio.sleep(0)

            self.assertIsNone(self.worker._curtain_blocked)
            self.assertEqual(evaluate_calls, 0)
            self.assertEqual(publish_calls, 0)

        asyncio.run(run_test())

    def test_light_curtain_change_is_ignored_when_crashed(self):
        self._assert_curtain_change_ignored(self.worker.set_crash_state, "test crash")


    def test_guard_waits_for_ack_after_interruption_clears(self):
        self.robot.set_curtain_state(True)
        self.robot.set_door_state(True)
        asyncio.run(self.worker._evaluate_safety_guard())

        self.robot.set_curtain_state(False)
        asyncio.run(self.worker._evaluate_safety_guard())

        safety = self.worker.get_safety_status()
        self.assertFalse(safety["active"])
        self.assertTrue(safety["motion_blocked"])
        self.assertTrue(safety["awaiting_ack"])
        self.assertEqual(self.robot.resume_calls, 0)

        result = asyncio.run(self.worker.acknowledge_safety_guard())

        self.assertTrue(result["accepted"])
        self.assertFalse(self.worker.get_safety_status()["motion_blocked"])
        self.assertEqual(self.robot.resume_calls, 1)

    def test_cabinet_door_open_blocks_without_light_curtain(self):
        self.robot.set_curtain_state(False)
        self.robot.set_door_state(False)
        self.robot.set_cabinet_door_state(
            {
                "top_closed": False,
                "bottom_closed": True,
                "closed": False,
                "top_open": True,
                "bottom_open": False,
                "open": True,
            }
        )

        asyncio.run(self.worker._evaluate_safety_guard())

        safety = self.worker.get_safety_status()
        self.assertTrue(safety["active"])
        self.assertEqual(safety["active_sources"], ["cabinet_doors"])
        self.assertEqual(self.robot.pause_calls, 1)

    def test_safety_guard_bypass_ignores_light_curtain_and_cabinet_doors(self):
        self.robot.set_curtain_state(True)
        self.robot.set_door_state(True)
        self.robot.set_cabinet_door_state(
            {
                "top_closed": False,
                "bottom_closed": True,
                "closed": False,
                "top_open": True,
                "bottom_open": False,
                "open": True,
            }
        )

        safety = asyncio.run(self.worker.set_safety_guard_bypassed(True))

        self.assertTrue(safety["bypassed"])
        self.assertFalse(safety["active"])
        self.assertFalse(safety["motion_blocked"])
        self.assertEqual(safety["active_sources"], [])
        self.assertTrue(safety["sources"]["light_curtain"]["bypassed"])
        self.assertTrue(safety["sources"]["cabinet_doors"]["bypassed"])
        self.assertEqual(self.robot.pause_calls, 0)
        self.assertTrue(self.robot.safety_guard_bypassed)


    def test_light_curtain_component_bypass_keeps_cabinet_doors_active(self):
        self.robot.set_curtain_state(True)
        self.robot.set_door_state(True)
        self.robot.set_cabinet_door_state(
            {
                "top_closed": False,
                "bottom_closed": True,
                "closed": False,
                "top_open": True,
                "bottom_open": False,
                "open": True,
            }
        )

        safety = asyncio.run(
            self.worker.set_safety_component_bypassed("light_curtain", True)
        )

        self.assertFalse(safety["bypassed"])
        self.assertTrue(safety["active"])
        self.assertEqual(safety["active_sources"], ["cabinet_doors"])
        self.assertTrue(safety["component_bypasses"]["light_curtain"])
        self.assertFalse(safety["component_bypasses"]["cabinet_doors"])
        self.assertTrue(safety["sources"]["light_curtain"]["bypassed"])
        self.assertFalse(safety["sources"]["cabinet_doors"]["bypassed"])
        self.assertTrue(self.robot.light_curtain_bypassed)

    def test_component_bypass_releases_ack_when_no_sources_remain(self):
        self.robot.set_curtain_state(True)
        self.robot.set_door_state(True)
        asyncio.run(self.worker._evaluate_safety_guard())

        safety = asyncio.run(
            self.worker.set_safety_component_bypassed("light_curtain", True)
        )

        self.assertFalse(safety["active"])
        self.assertFalse(safety["motion_blocked"])
        self.assertFalse(safety["awaiting_ack"])
        self.assertEqual(safety["active_sources"], [])
        self.assertTrue(safety["sources"]["light_curtain"]["bypassed"])
        self.assertEqual(self.robot.pause_calls, 1)
        self.assertEqual(self.robot.resume_calls, 1)


    def test_global_bypass_overrides_component_bypasses(self):
        self.robot.set_curtain_state(True)
        self.robot.set_door_state(True)
        self.robot.set_cabinet_door_state(
            {
                "top_closed": False,
                "bottom_closed": True,
                "closed": False,
                "top_open": True,
                "bottom_open": False,
                "open": True,
            }
        )

        safety = asyncio.run(self.worker.set_safety_guard_bypassed(True))

        self.assertEqual(safety["active_sources"], [])
        self.assertTrue(safety["sources"]["light_curtain"]["bypassed"])
        self.assertTrue(safety["sources"]["cabinet_doors"]["bypassed"])
        self.assertFalse(safety["component_bypasses"]["light_curtain"])
        self.assertFalse(safety["component_bypasses"]["cabinet_doors"])

    def test_unknown_component_bypass_raises(self):
        with self.assertRaises(ValueError):
            asyncio.run(self.worker.set_safety_component_bypassed("scale", True))

    def test_overlapping_sources_require_all_clear_before_ack(self):
        self.robot.set_curtain_state(True)
        self.robot.set_door_state(True)
        self.robot.set_cabinet_door_state(
            {
                "top_closed": False,
                "bottom_closed": True,
                "closed": False,
                "top_open": True,
                "bottom_open": False,
                "open": True,
            }
        )
        asyncio.run(self.worker._evaluate_safety_guard())

        safety = self.worker.get_safety_status()
        self.assertEqual(safety["active_sources"], ["light_curtain", "cabinet_doors"])

        self.robot.set_curtain_state(False)
        asyncio.run(self.worker._evaluate_safety_guard())
        result = asyncio.run(self.worker.acknowledge_safety_guard())

        self.assertFalse(result["accepted"])
        self.assertEqual(self.worker.get_safety_status()["active_sources"], ["cabinet_doors"])
        self.assertEqual(self.robot.resume_calls, 0)

        self.robot.set_cabinet_door_state(
            {
                "top_closed": True,
                "bottom_closed": True,
                "closed": True,
                "top_open": False,
                "bottom_open": False,
                "open": False,
            }
        )
        result = asyncio.run(self.worker.acknowledge_safety_guard())

        self.assertTrue(result["accepted"])
        self.assertEqual(self.robot.resume_calls, 1)


if __name__ == "__main__":
    unittest.main()
