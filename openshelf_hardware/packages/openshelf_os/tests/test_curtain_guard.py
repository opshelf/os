"""Curtain guard unit tests with lightweight dependency stubs."""
# ruff: noqa: E402

import asyncio
import pathlib
import sys
import unittest
import types
import logging

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

# Stub the hardware package tree to avoid pulling real dependencies.
class _DummySharedModbusController:
    def __init__(self, *_, **__):
        self.client = None

    async def connect(self, *_, **__):
        return None

    async def close(self, *_, **__):
        return None


class _DummyMotor:
    def __init__(self, *_, safety=None, **__):
        self._pos = 0.0
        self.stop_calls = 0
        self.moves = []
        self.safety = safety

    async def configure(self, *_, **__):
        return None

    async def home(self, *_, **__):
        return None

    async def move_to_position(self, value, *_, **__):
        if self.safety and getattr(self.safety, "blocked", False):
            await self.safety.wait_clear()
        await asyncio.sleep(0)
        if self.safety and getattr(self.safety, "blocked", False):
            await self.safety.wait_clear()
        self.moves.append(value)
        self._pos = value

    async def move_to_position_no_wait(self, value, *_, **__):
        if self.safety and getattr(self.safety, "blocked", False):
            await self.safety.wait_clear()
        self.moves.append(value)
        self._pos = value

    async def get_position(self):
        return self._pos

    async def stop(self):
        self.stop_calls += 1
        return None


class _DummyCurtainSensor:
    def __init__(self, *_, **__):
        pass

    def cleanup(self):
        return None


class _DummyCurtainMonitor:
    def __init__(self, *_, **__):
        pass

    async def start(self):
        return None

    async def stop(self):
        return None


class _DummyScale:
    def __init__(self, *_, **__):
        pass

    async def configure(self, *_, **__):
        return None

    async def tare(self, *_, **__):
        return None

    async def wait_for_stable_weight(self, *_, **__):
        return None


class _DummyCurtainSafety:
    def __init__(self):
        self._clear = asyncio.Event()
        self.blocked = False
        self._clear.set()

    def block(self):
        self.blocked = True
        self._clear.clear()

    def unblock(self):
        self.blocked = False
        self._clear.set()

    async def wait_clear(self):
        await self._clear.wait()


hardware_module = types.ModuleType("hardware")
hardware_controller = types.SimpleNamespace(SharedModbusController=_DummySharedModbusController)
hardware_motor = types.SimpleNamespace(Motor=_DummyMotor)
hardware_motor.Servo = _DummyMotor
hardware_motor.MotorStatus = types.SimpleNamespace
hardware_motor.MOTOR_WAIT_TIMEOUT = 0
hardware_curtain = types.SimpleNamespace(
    CurtainSafety=_DummyCurtainSafety,
    CurtainSensor=_DummyCurtainSensor,
    CurtainMonitor=_DummyCurtainMonitor,
)
hardware_scale = types.SimpleNamespace(Scale=_DummyScale)
hardware_toolcam = types.SimpleNamespace(ToolCamera=object)
hardware_iocam = types.SimpleNamespace(AFSCam=object)
hardware_laser = types.SimpleNamespace(LaserSensor=object)

hardware_module.controller = hardware_controller
hardware_module.motor = hardware_motor
hardware_module.curtain = hardware_curtain
hardware_module.scale = hardware_scale
hardware_module.toolcam = hardware_toolcam
hardware_module.iocam = hardware_iocam
hardware_module.laser = hardware_laser

sys.modules.setdefault("hardware", hardware_module)
sys.modules.setdefault("hardware.controller", hardware_controller)
sys.modules.setdefault("hardware.motor", hardware_motor)
sys.modules.setdefault("hardware.curtain", hardware_curtain)
sys.modules.setdefault("hardware.scale", hardware_scale)
sys.modules.setdefault("hardware.toolcam", hardware_toolcam)
sys.modules.setdefault("hardware.iocam", hardware_iocam)
sys.modules.setdefault("hardware.laser", hardware_laser)

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
from openshelf_os.robot import Robot


class FakeRobot:
    def __init__(self) -> None:
        self._door_state = False
        self.pause_calls = 0
        self.resume_calls = 0

    def set_door_state(self, state):
        self._door_state = state

    async def refresh_door_state(self):
        return self._door_state

    def pause_for_curtain(self):
        self.pause_calls += 1

    def resume_after_curtain(self):
        self.resume_calls += 1


class CurtainGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.robot = FakeRobot()
        self.worker = RobotWorker(self.robot)

    def test_guard_blocks_when_curtain_and_door_open(self):
        self.worker._curtain_blocked = True
        self.robot.set_door_state(True)

        asyncio.run(self.worker._evaluate_curtain_guard())

        self.assertTrue(self.worker._curtain_guard_active)
        self.assertEqual(self.robot.pause_calls, 1)
        self.assertEqual(self.robot.resume_calls, 0)

    def test_guard_blocks_when_door_unknown(self):
        self.worker._curtain_blocked = True
        self.robot.set_door_state(None)

        asyncio.run(self.worker._evaluate_curtain_guard())

        self.assertFalse(self.worker._curtain_guard_active)
        self.assertEqual(self.robot.pause_calls, 0)

    def test_guard_resumes_when_curtain_clears(self):
        self.worker._curtain_blocked = True
        self.robot.set_door_state(True)
        asyncio.run(self.worker._evaluate_curtain_guard())

        self.worker._curtain_blocked = False
        asyncio.run(self.worker._evaluate_curtain_guard())

        self.assertFalse(self.worker._curtain_guard_active)
        self.assertEqual(self.robot.resume_calls, 1)

    def test_demo_cycling_skips_ack(self):
        self.worker._demo_cycling = True
        self.worker._curtain_blocked = True
        self.robot.set_door_state(True)
        asyncio.run(self.worker._evaluate_curtain_guard())

        self.assertTrue(self.worker._curtain_guard_active)
        self.assertEqual(self.robot.pause_calls, 1)
        self.assertFalse(self.worker._awaiting_curtain_ack)

        self.worker._curtain_blocked = False
        asyncio.run(self.worker._evaluate_curtain_guard())

        self.assertFalse(self.worker._curtain_guard_active)
        self.assertEqual(self.robot.resume_calls, 1)
        self.assertFalse(self.worker._awaiting_curtain_ack)

    def test_demo_cycling_off_requires_ack(self):
        self.worker._demo_cycling = False
        self.worker._curtain_blocked = True
        self.robot.set_door_state(True)
        asyncio.run(self.worker._evaluate_curtain_guard())

        self.assertTrue(self.worker._awaiting_curtain_ack)


class RobotCurtainPauseTests(unittest.TestCase):
    def setUp(self) -> None:
        safety = _DummyCurtainSafety()
        stage = _DummyMotor(safety=safety)
        rotate = _DummyMotor(safety=safety)

        robot = Robot.__new__(Robot)
        robot._curtain_safety = safety
        robot._tool = None
        robot._io = None
        robot._logger = logging.getLogger(__name__).getChild("robot.test")
        self.robot = robot
        self.safety = safety
        self.stage = stage
        self.rotate = rotate

    def test_pause_blocks_safety(self):
        self.robot.pause_for_curtain()
        self.assertTrue(self.safety.blocked)

    def test_resume_unblocks_safety(self):
        self.robot.pause_for_curtain()
        self.robot.resume_after_curtain()
        self.assertFalse(self.safety.blocked)

    def test_stage_move_waits_for_curtain_resume(self):
        async def run_sequence():
            self.safety.unblock()
            move_task = asyncio.create_task(self.stage.move_to_position(5))
            await asyncio.sleep(0)
            self.robot.pause_for_curtain()
            await asyncio.sleep(0)
            self.robot.resume_after_curtain()
            await move_task

        asyncio.run(run_sequence())
        self.assertEqual(self.stage._pos, 5)
        self.assertFalse(self.safety.blocked)


if __name__ == "__main__":
    unittest.main()
