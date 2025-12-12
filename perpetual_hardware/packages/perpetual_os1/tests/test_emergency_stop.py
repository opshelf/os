"""Emergency stop unit tests."""
# ruff: noqa: E402

import asyncio
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


class _DummyCurtainSafety:
    def __init__(self):
        self.blocked = False

    async def wait_clear(self):
        pass


hardware_module = types.ModuleType("hardware")
hardware_controller = types.SimpleNamespace(SharedModbusController=_DummySharedModbusController)
hardware_motor = types.SimpleNamespace(Motor=_DummyMotor, CurtainSafety=_DummyCurtainSafety, Servo=_DummyMotor, MotorStatus=types.SimpleNamespace, MOTOR_WAIT_TIMEOUT=0)
hardware_curtain = types.SimpleNamespace(CurtainSensor=object, CurtainMonitor=object)
hardware_scale = types.SimpleNamespace(Scale=object)
hardware_toolcam = types.SimpleNamespace(ToolCamera=object)
hardware_iocam = types.SimpleNamespace(AFSCam=object)

hardware_module.controller = hardware_controller
hardware_module.motor = hardware_motor
hardware_module.curtain = hardware_curtain
hardware_module.scale = hardware_scale
hardware_module.toolcam = hardware_toolcam
hardware_module.iocam = hardware_iocam

sys.modules.setdefault("hardware", hardware_module)
sys.modules.setdefault("hardware.controller", hardware_controller)
sys.modules.setdefault("hardware.motor", hardware_motor)
sys.modules.setdefault("hardware.curtain", hardware_curtain)
sys.modules.setdefault("hardware.scale", hardware_scale)
sys.modules.setdefault("hardware.toolcam", hardware_toolcam)
sys.modules.setdefault("hardware.iocam", hardware_iocam)


class _DummyNATS:
    def __init__(self, *_, **__):
        pass

    async def connect(self, *_, **__):
        return None

    def jetstream(self):
        return _DummyJetStream()

    async def publish(self, *_, **__):
        return None


class _DummyJetStream:
    async def add_stream(self, *_, **__):
        return None

    async def pull_subscribe(self, *_, **__):
        return _DummySubscription()

    async def purge_stream(self, *_, **__):
        return None


class _DummySubscription:
    async def fetch(self, *_, **__):
        return []


class _DummyMsg:
    def __init__(self):
        self.acked = False

    async def ack(self):
        self.acked = True


nats_module = types.ModuleType("nats")
nats_aio = types.ModuleType("nats.aio")
nats_aio_client = types.SimpleNamespace(Client=_DummyNATS)
nats_aio_msg = types.SimpleNamespace(Msg=_DummyMsg)
nats_errors = types.SimpleNamespace(TimeoutError=asyncio.TimeoutError)
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
        self.curtain_paused = False

    def pause_for_curtain(self):
        self.curtain_paused = True

    def resume_after_curtain(self):
        self.curtain_paused = False

    async def get_status(self):
        return None

    def register_curtain_listener(self, cb):
        pass


class MockRobotClient:
    async def connect(self):
        pass


class EmergencyStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_emergency_stop_sets_crash_state_and_pauses_curtain(self):
        from perpetual_os1.robot_worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        worker._client = MockRobotClient()
        worker._nc = _DummyNATS()
        worker._js = _DummyJetStream()

        self.assertFalse(worker._is_crashed)
        self.assertFalse(robot.curtain_paused)

        await worker.emergency_stop("test reason")

        self.assertTrue(worker._is_crashed)
        self.assertEqual(worker._crash_reason, "test reason")
        self.assertTrue(robot.curtain_paused)

        RobotWorker._instance = None

    async def test_emergency_stop_is_instant(self):
        from perpetual_os1.robot_worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        worker._client = MockRobotClient()
        worker._nc = _DummyNATS()
        worker._js = _DummyJetStream()

        start = asyncio.get_event_loop().time()
        await worker.emergency_stop("test")
        elapsed = asyncio.get_event_loop().time() - start

        self.assertLess(elapsed, 0.1)
        self.assertTrue(worker._is_crashed)
        self.assertTrue(robot.curtain_paused)

        RobotWorker._instance = None

    async def test_clear_crash_state_resumes_curtain(self):
        from perpetual_os1.robot_worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        worker._client = MockRobotClient()
        worker._nc = _DummyNATS()
        worker._js = _DummyJetStream()

        await worker.emergency_stop("test")
        self.assertTrue(robot.curtain_paused)

        worker.clear_crash_state()
        self.assertFalse(worker._is_crashed)
        self.assertFalse(robot.curtain_paused)

        RobotWorker._instance = None

    async def test_emergency_stop_cancels_running_task(self):
        from perpetual_os1.robot_worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        worker._client = MockRobotClient()
        worker._nc = _DummyNATS()
        worker._js = _DummyJetStream()

        task_cancelled = asyncio.Event()

        async def slow_command():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                task_cancelled.set()
                raise

        worker._current_task = asyncio.create_task(slow_command())
        msg = _DummyMsg()
        worker._msg = msg

        await worker.emergency_stop("test")

        await asyncio.sleep(0.1)
        self.assertTrue(task_cancelled.is_set())
        self.assertTrue(msg.acked)
        self.assertIsNone(worker._msg)

        RobotWorker._instance = None

    async def test_emergency_stop_clears_command_state_in_background(self):
        from perpetual_os1.robot_worker import RobotWorker
        from perpetual_os1.robot import HomeCommand

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        worker._client = MockRobotClient()
        worker._nc = _DummyNATS()
        worker._js = _DummyJetStream()

        mock_command = HomeCommand(command="home")
        worker._current_command = mock_command
        worker._command_started_at = 12345.0

        await worker.emergency_stop("test")

        self.assertEqual(worker._current_command, mock_command)

        await asyncio.sleep(0.1)

        self.assertIsNone(worker._current_task)
        self.assertIsNone(worker._current_command)
        self.assertIsNone(worker._command_started_at)
        self.assertIsNone(worker._msg)

        RobotWorker._instance = None


if __name__ == "__main__":
    unittest.main()
