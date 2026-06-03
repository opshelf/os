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


class _DummyGpioSubscription:
    def __init__(self, did_close=None):
        self._did_close = did_close
        self._closed = False

    def close(self):
        if self._closed is True:
            return
        self._closed = True
        if self._did_close is not None:
            self._did_close()


class _DummyGpioEvent:
    def __init__(self, name, value, raw_value):
        self.name = name
        self.value = value
        self.raw_value = raw_value
        self.timestamp = 0.0


class _DummyGpioPinSpec:
    def __init__(
        self,
        *,
        name,
        board,
        pin,
        direction,
        expander=None,
        inverted=False,
        **_,
    ):
        self.name = name
        self.board = board
        self.pin = pin
        self.direction = direction
        self.expander = expander
        self.inverted = inverted

    def to_logical(self, raw_value):
        return not raw_value if self.inverted is True else raw_value

    def to_raw(self, value):
        return not value if self.inverted is True else value


class _DummyDigitalInput:
    def __init__(self, manager, spec):
        self._manager = manager
        self._spec = spec
        self._subscribers = []

    @property
    def name(self):
        return self._spec.name

    @property
    def pin(self):
        return self._spec.pin

    @property
    def board(self):
        return self._spec.board

    def read(self):
        return self._spec.to_logical(self._manager._raw_values.get(self.name, False))

    def subscribe(self, callback, *, emit_initial=False):
        self._subscribers.append(callback)
        if emit_initial is True:
            callback(_DummyGpioEvent(self.name, self.read(), self._manager._raw_values.get(self.name, False)))

        def did_close():
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return _DummyGpioSubscription(did_close)

    def _emit(self):
        raw_value = self._manager._raw_values.get(self.name, False)
        value = self._spec.to_logical(raw_value)
        event = _DummyGpioEvent(self.name, value, raw_value)
        for callback in list(self._subscribers):
            callback(event)


class _DummyDigitalOutput:
    def __init__(self, manager, spec):
        self._manager = manager
        self._spec = spec

    def write(self, value):
        self._manager._raw_values[self._spec.name] = self._spec.to_raw(value)

    def read(self):
        return self._spec.to_logical(self._manager._raw_values.get(self._spec.name, False))


class _DummyGpioManager:
    def __init__(self, pins, *_, fake=False, **__):
        self._pins = dict(pins)
        self._fake = fake
        self._inputs = {}
        self._outputs = {}
        self._raw_values = {name: False for name in self._pins}

    def has_pin(self, name):
        return name in self._pins

    def input(self, name):
        if name not in self._inputs:
            self._inputs[name] = _DummyDigitalInput(self, self._pins[name])
        return self._inputs[name]

    def output(self, name):
        if name not in self._outputs:
            self._outputs[name] = _DummyDigitalOutput(self, self._pins[name])
        return self._outputs[name]

    def set_fake_input(self, name, value, *, raw=False):
        spec = self._pins[name]
        self._raw_values[name] = value if raw is True else spec.to_raw(value)
        if name in self._inputs:
            self._inputs[name]._emit()


class _DummyCurtainSensor:
    def __init__(self, *, input_device=None, **__):
        self._input = input_device
        self.pin = getattr(input_device, "pin", None)

    def read(self):
        if self._input is None:
            return False
        return self._input.read()

    def subscribe(self, on_change):
        if self._input is None:
            raise RuntimeError("input device not configured")

        def did_change(event):
            on_change(event.value)

        return self._input.subscribe(did_change)

    def cleanup(self):
        return None


class _DummyCurtainMonitor:
    def __init__(self, sensor, *, on_change, **__):
        self._sensor = sensor
        self._on_change = on_change
        self._subscription = None
        self._last = None

    async def start(self):
        self._last = self._sensor.read()
        self._on_change(self._last)
        self._subscription = self._sensor.subscribe(self._handle_change)

    async def stop(self):
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None

    def _handle_change(self, state):
        if state == self._last:
            return
        self._last = state
        self._on_change(state)


class _DummyScale:
    def __init__(self, *_, **__):
        pass

    async def configure(self, *_, **__):
        return None

    async def tare(self, *_, **__):
        return None

    async def wait_for_stable_weight(self, *_, **__):
        return None


class _DummyMotionSafetyInterlock:
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
    MotionSafetyInterlock=_DummyMotionSafetyInterlock,
    CurtainSensor=_DummyCurtainSensor,
    CurtainMonitor=_DummyCurtainMonitor,
)
hardware_scale = types.SimpleNamespace(Scale=_DummyScale)
hardware_toolcam = types.SimpleNamespace(ToolCamera=object)
hardware_laser = types.SimpleNamespace(LaserSensor=object)
hardware_gpio = types.ModuleType("hardware.gpio")
hardware_gpio_types = types.SimpleNamespace(
    DigitalInput=_DummyDigitalInput,
    DigitalOutput=_DummyDigitalOutput,
    GpioEvent=_DummyGpioEvent,
    GpioSubscription=_DummyGpioSubscription,
    GpioPinSpec=_DummyGpioPinSpec,
    PinDirection=types.SimpleNamespace(INPUT="input", OUTPUT="output"),
    PinPull=types.SimpleNamespace(UP="up", DOWN="down", NONE="none"),
)
hardware_gpio.GpioManager = _DummyGpioManager
hardware_gpio.GpioPinSpec = _DummyGpioPinSpec
hardware_gpio.types = hardware_gpio_types

hardware_module.controller = hardware_controller
hardware_module.motor = hardware_motor
hardware_module.curtain = hardware_curtain
hardware_module.scale = hardware_scale
hardware_module.toolcam = hardware_toolcam
hardware_module.laser = hardware_laser
hardware_module.gpio = hardware_gpio
sys.modules.setdefault("hardware", hardware_module)
sys.modules.setdefault("hardware.controller", hardware_controller)
sys.modules.setdefault("hardware.motor", hardware_motor)
sys.modules.setdefault("hardware.curtain", hardware_curtain)
sys.modules.setdefault("hardware.scale", hardware_scale)
sys.modules.setdefault("hardware.toolcam", hardware_toolcam)
sys.modules.setdefault("hardware.laser", hardware_laser)
sys.modules.setdefault("hardware.gpio", hardware_gpio)
sys.modules.setdefault("hardware.gpio.types", hardware_gpio_types)
sys.modules.setdefault("hardware.servo", types.SimpleNamespace(Servo=_DummyMotor))

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

from openshelf_os.robot.worker import RobotWorker, _transform_worker_status_payload
from openshelf_os.robot import Robot

if sys.modules.get("hardware") is hardware_module:
    for module_name in (
        "hardware",
        "hardware.controller",
        "hardware.motor",
        "hardware.curtain",
        "hardware.scale",
        "hardware.toolcam",
        "hardware.laser",
        "hardware.gpio",
        "hardware.gpio.types",
        "hardware.servo",
        "openshelf_os.iobox",
    ):
        sys.modules.pop(module_name, None)


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

    def set_curtain_state(self, state):
        self._curtain_state = state

    def set_door_state(self, state):
        self._door_state = state

    def set_cabinet_door_state(self, state):
        self._cabinet_door_state = state

    def get_curtain_state(self):
        return self._curtain_state

    async def refresh_door_state(self):
        return self._door_state

    def get_cabinet_door_state(self):
        return self._cabinet_door_state

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

    def test_status_transform_preserves_io_door_state(self):
        payload = _transform_worker_status_payload(
            {
                "robot_status": {
                    "gantry": {},
                    "tool": {},
                    "state": {},
                    "storage": {},
                    "io_door_open": True,
                }
            }
        )

        self.assertTrue(payload["robot_status"]["io_door_open"])

    def test_idle_poll_publishes_status_when_io_door_changes(self):
        async def run_test():
            self.robot.set_curtain_state(False)
            self.robot.set_door_state(False)
            await self.worker._evaluate_safety_guard()

            publish_calls = 0

            async def did_publish_status():
                nonlocal publish_calls
                publish_calls += 1
                self.worker._running = False

            self.worker._publish_status = did_publish_status
            self.robot.set_door_state(True)
            self.worker._running = True

            await asyncio.wait_for(self.worker.process_commands(), timeout=2)

            self.assertEqual(publish_calls, 1)

        asyncio.run(run_test())


class RobotCurtainPauseTests(unittest.TestCase):
    def setUp(self) -> None:
        safety = _DummyMotionSafetyInterlock()
        stage = _DummyMotor(safety=safety)
        rotate = _DummyMotor(safety=safety)

        robot = Robot.__new__(Robot)
        robot._motion_safety = safety
        robot._tool = None
        robot._io = None
        robot._logger = logging.getLogger(__name__).getChild("robot.test")
        self.robot = robot
        self.safety = safety
        self.stage = stage
        self.rotate = rotate

    def test_pause_blocks_safety(self):
        self.robot.block_motion_for_safety()
        self.assertTrue(self.safety.blocked)

    def test_resume_unblocks_safety(self):
        self.robot.block_motion_for_safety()
        self.robot.release_motion_safety()
        self.assertFalse(self.safety.blocked)

    def test_stage_move_waits_for_curtain_resume(self):
        async def run_sequence():
            self.safety.unblock()
            move_task = asyncio.create_task(self.stage.move_to_position(5))
            await asyncio.sleep(0)
            self.robot.block_motion_for_safety()
            await asyncio.sleep(0)
            self.robot.release_motion_safety()
            await move_task

        asyncio.run(run_sequence())
        self.assertEqual(self.stage._pos, 5)
        self.assertFalse(self.safety.blocked)


if __name__ == "__main__":
    unittest.main()
