"""Emergency stop unit tests."""
# ruff: noqa: E402, PLC0415

import asyncio
import json
import pathlib
import sys
import unittest
import types

BASE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(BASE.parents[1] / "src"))
sys.path.insert(0, str(BASE.parents[3] / "packages" / "hardware" / "src"))

sys.modules.setdefault("sentry_sdk", types.SimpleNamespace(
    init=lambda **kwargs: None,
    capture_exception=lambda e: None,
    capture_message=lambda m, **kw: None,
    push_scope=lambda: types.SimpleNamespace(__enter__=lambda s: types.SimpleNamespace(set_tag=lambda *a: None, set_context=lambda *a: None, set_extra=lambda *a: None), __exit__=lambda *a: None),
    flush=lambda **kw: None,
))

try:
    import pydantic as _pydantic_available  # noqa: F401
except Exception:
    class _DummyBaseModel:
        def __init__(self, *args, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

        @classmethod
        def model_validate(cls, value):
            return value

        def model_dump(self, *args, **kwargs):
            return {}

        def model_dump_json(self):
            return "{}"

    def _identity_decorator(*args, **kwargs):
        return lambda fn: fn

    class _DummyTypeAdapter:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, model):
            self.model = model

        def validate_json(self, data):
            return data

    pydantic_stub = types.SimpleNamespace(
        BaseModel=_DummyBaseModel,
        Field=lambda *args, **kwargs: kwargs.get("default", None),
        TypeAdapter=_DummyTypeAdapter,
    )
    sys.modules.setdefault("pydantic", pydantic_stub)

try:
    import pymodbus as _pymodbus_available  # noqa: F401
except Exception:
    pymodbus_stub = types.SimpleNamespace(
        FramerType=types.SimpleNamespace(RTU=None),
        client=types.SimpleNamespace(AsyncModbusSerialClient=object),
        exceptions=types.SimpleNamespace(ModbusIOException=Exception, ModbusException=Exception),
    )
    sys.modules.setdefault("pymodbus", pymodbus_stub)
    sys.modules.setdefault("pymodbus.client", pymodbus_stub.client)
    sys.modules.setdefault("pymodbus.exceptions", pymodbus_stub.exceptions)

sys.modules.setdefault("cv2", types.SimpleNamespace())


class _DummySharedModbusController:
    def __init__(self, *_, **__):
        self.client = None

    async def connect(self, *_, **__):
        return None

    async def close(self, *_, **__):
        return None


class _DummyMotor:
    def __init__(self, *_, **__):
        self._pos = 0.0
        self.stop_calls = 0

    async def configure(self, *_, **__):
        return None

    async def home(self, *_, **__):
        return None

    async def move_to_position(self, value, *_, **__):
        self._pos = value

    async def move_to_position_no_wait(self, value, *_, **__):
        self._pos = value

    async def get_position(self):
        return self._pos

    async def stop(self):
        self.stop_calls += 1
        return None


class _DummyMotionSafetyInterlock:
    def __init__(self):
        self.blocked = False

    async def wait_clear(self):
        pass


hardware_module = types.ModuleType("hardware")
hardware_controller = types.SimpleNamespace(SharedModbusController=_DummySharedModbusController)
hardware_motor = types.SimpleNamespace(Motor=_DummyMotor, Servo=_DummyMotor, MotorStatus=types.SimpleNamespace, MOTOR_WAIT_TIMEOUT=0)
hardware_servo = types.SimpleNamespace(Servo=_DummyMotor)
hardware_curtain = types.SimpleNamespace(
    MotionSafetyInterlock=_DummyMotionSafetyInterlock,
    CurtainSensor=object,
    CurtainMonitor=object,
)
hardware_scale = types.SimpleNamespace(Scale=object)
hardware_toolcam = types.SimpleNamespace(ToolCamera=object)
hardware_laser = types.SimpleNamespace(LaserSensor=object)
hardware_gpio = types.ModuleType("hardware.gpio")
hardware_gpio_types = types.ModuleType("hardware.gpio.types")
hardware_gpio_types.DigitalInput = object
hardware_gpio_types.DigitalOutput = object
hardware_gpio_types.GpioEvent = object
hardware_gpio_types.GpioSubscription = object
hardware_gpio.types = hardware_gpio_types

hardware_module.controller = hardware_controller
hardware_module.motor = hardware_motor
hardware_module.servo = hardware_servo
hardware_module.curtain = hardware_curtain
hardware_module.scale = hardware_scale
hardware_module.toolcam = hardware_toolcam
hardware_module.laser = hardware_laser
hardware_module.gpio = hardware_gpio

sys.modules.setdefault("hardware", hardware_module)
sys.modules.setdefault("hardware.controller", hardware_controller)
sys.modules.setdefault("hardware.motor", hardware_motor)
sys.modules.setdefault("hardware.servo", hardware_servo)
sys.modules.setdefault("hardware.curtain", hardware_curtain)
sys.modules.setdefault("hardware.scale", hardware_scale)
sys.modules.setdefault("hardware.toolcam", hardware_toolcam)
sys.modules.setdefault("hardware.laser", hardware_laser)
sys.modules.setdefault("hardware.gpio", hardware_gpio)
sys.modules.setdefault("hardware.gpio.types", hardware_gpio_types)


try:
    import serial_asyncio as _serial_asyncio_available  # noqa: F401
except Exception:
    sys.modules.setdefault("serial_asyncio", types.SimpleNamespace())

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


class MockRobot:
    def __init__(self):
        self.safety_paused = False

    def block_motion_for_safety(self):
        self.safety_paused = True

    def release_motion_safety(self):
        self.safety_paused = False

    async def get_status(self):
        return None

    def register_light_curtain_listener(self, cb):
        pass

    def register_cabinet_door_listener(self, cb):
        pass


class FakeEstopInput:
    def __init__(self):
        self.value = False

    def read(self):
        return self.value


class FailingEstopInput:
    def read(self):
        raise RuntimeError("i2c unavailable")


class FailingGpio:
    def has_pin(self, name):
        return name == "estop"

    def input(self, name):
        raise RuntimeError(f"{name} unavailable")


class EmergencyStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_emergency_stop_sets_crash_state_and_pauses_curtain(self):
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)

        self.assertFalse(worker._is_emergency_stopped)
        self.assertFalse(worker._is_crashed)
        self.assertFalse(robot.safety_paused)

        await worker.emergency_stop("test reason")

        self.assertTrue(worker._is_emergency_stopped)
        self.assertEqual(worker._emergency_stop_reason, "test reason")
        self.assertFalse(worker._is_crashed)
        self.assertTrue(robot.safety_paused)

        payload = json.loads(await worker.get_status_snapshot(force_refresh=True))
        self.assertTrue(payload["is_emergency_stopped"])
        self.assertFalse(payload["is_emergency_stop_active"])
        self.assertEqual(payload["emergency_stop_reason"], "test reason")

        RobotWorker._instance = None

    async def test_emergency_stop_is_instant(self):
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)

        start = asyncio.get_event_loop().time()
        await worker.emergency_stop("test")
        elapsed = asyncio.get_event_loop().time() - start

        self.assertLess(elapsed, 0.1)
        self.assertTrue(worker._is_emergency_stopped)
        self.assertFalse(worker._is_crashed)
        self.assertTrue(robot.safety_paused)

        RobotWorker._instance = None

    async def test_clear_crash_state_resumes_curtain(self):
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)

        robot.block_motion_for_safety()
        worker.set_crash_state("test")
        self.assertTrue(robot.safety_paused)

        worker.clear_crash_state()
        self.assertFalse(worker._is_crashed)
        self.assertFalse(robot.safety_paused)

        RobotWorker._instance = None

    async def test_emergency_stop_cancels_running_task(self):
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)

        task_cancelled = asyncio.Event()

        async def slow_command():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                task_cancelled.set()
                raise

        worker._current_task = asyncio.create_task(slow_command())

        await worker.emergency_stop("test")

        await asyncio.sleep(0.1)
        self.assertTrue(task_cancelled.is_set())
        self.assertIsNone(worker._current_task)

        RobotWorker._instance = None

    async def test_emergency_stop_clears_command_state_in_background(self):
        from openshelf_os.robot.worker import RobotWorker
        from openshelf_os.robot.commands import HomeCommand

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)

        mock_command = HomeCommand(command="home")
        worker._current_command = mock_command
        worker._current_operation = worker._snapshot_command(mock_command)
        worker._queued_commands.append(worker._current_operation)
        worker._command_started_at = 12345.0

        await worker.emergency_stop("test")

        self.assertEqual(worker._current_command, mock_command)

        await asyncio.sleep(0.1)

        self.assertIsNone(worker._current_task)
        self.assertIsNone(worker._current_command)
        self.assertIsNone(worker._current_operation)
        self.assertIsNone(worker._command_started_at)
        self.assertEqual(worker._queued_commands, [])

        RobotWorker._instance = None


    async def test_estop_monitor_fails_safe_when_configured_input_setup_fails(self):
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        robot._gpio = FailingGpio()
        RobotWorker._instance = None
        worker = RobotWorker(robot)

        await worker._start_estop_monitor()

        self.assertTrue(worker._is_emergency_stopped)
        self.assertEqual(worker._emergency_stop_reason, "Emergency stop input unavailable")
        self.assertFalse(worker._is_crashed)
        self.assertTrue(worker._is_estop_active)
        self.assertTrue(robot.safety_paused)
        self.assertIsNone(worker._estop_monitor_task)

        RobotWorker._instance = None

    async def test_estop_monitor_fails_safe_when_configured_input_read_fails(self):
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        emergency_stop_calls = []
        original_emergency_stop = worker.emergency_stop

        async def emergency_stop(reason):
            emergency_stop_calls.append(reason)
            await original_emergency_stop(reason)

        worker.emergency_stop = emergency_stop
        worker._running = True
        monitor_task = asyncio.create_task(worker._monitor_estop(FailingEstopInput()))

        try:
            await asyncio.sleep(0.16)

            self.assertTrue(worker._is_emergency_stopped)
            self.assertEqual(worker._emergency_stop_reason, "Emergency stop input unavailable")
            self.assertFalse(worker._is_crashed)
            self.assertTrue(worker._is_estop_active)
            self.assertTrue(robot.safety_paused)
            self.assertEqual(emergency_stop_calls, ["Emergency stop input unavailable"])

            worker.clear_emergency_stop_state()

            self.assertTrue(worker._is_emergency_stopped)
            self.assertEqual(worker._emergency_stop_reason, "Emergency stop input unavailable")
        finally:
            worker._running = False
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
            RobotWorker._instance = None

    async def test_estop_monitor_crashes_on_active_input(self):
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        estop_input = FakeEstopInput()
        worker._running = True
        monitor_task = asyncio.create_task(worker._monitor_estop(estop_input))

        try:
            await asyncio.sleep(0.06)
            self.assertFalse(worker._is_crashed)

            estop_input.value = True
            await asyncio.sleep(0.1)

            self.assertTrue(worker._is_emergency_stopped)
            self.assertEqual(worker._emergency_stop_reason, "Physical emergency stop triggered")
            self.assertFalse(worker._is_crashed)
            self.assertTrue(worker._is_estop_active)
            self.assertTrue(robot.safety_paused)

            estop_input.value = False
            await asyncio.sleep(0.1)

            self.assertTrue(worker._is_emergency_stopped)
            self.assertFalse(worker._is_estop_active)
            self.assertTrue(robot.safety_paused)

            worker.clear_emergency_stop_state()

            self.assertFalse(worker._is_emergency_stopped)
            self.assertIsNone(worker._emergency_stop_reason)
            self.assertFalse(robot.safety_paused)
        finally:
            worker._running = False
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
            RobotWorker._instance = None

if __name__ == "__main__":
    unittest.main()
