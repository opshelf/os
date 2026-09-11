"""Emergency stop unit tests."""
# ruff: noqa: E402, PLC0415

import asyncio
import json
import pathlib
import sys
import unittest
import types
import uuid
from unittest.mock import patch


def assert_correlated_success(case, event, expected):
    command_id = event.get("command_id")
    case.assertIsInstance(command_id, str)
    uuid.UUID(command_id)
    normalized = dict(event)
    normalized.pop("command_id")
    case.assertEqual(normalized, expected)


def assert_correlated_failure(case, event, expected, *, code="command_interrupted"):
    command_id = event.get("command_id")
    case.assertIsInstance(command_id, str)
    uuid.UUID(command_id)
    case.assertEqual(event.get("message"), "failed")
    case.assertEqual(event.get("code"), code)
    normalized = dict(event)
    normalized.pop("command_id")
    normalized.pop("code")
    normalized["message"] = "error"
    case.assertEqual(normalized, expected)

BASE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(BASE.parents[1] / "src"))
sys.path.insert(0, str(BASE.parents[3] / "packages" / "hardware" / "src"))

sys.modules.setdefault("sentry_sdk", types.SimpleNamespace(
    init=lambda **kwargs: None,
    capture_exception=lambda e: None,
    capture_message=lambda m, **kw: None,
    new_scope=lambda: types.SimpleNamespace(__enter__=lambda s: types.SimpleNamespace(set_tag=lambda *a: None, set_context=lambda *a: None, set_extra=lambda *a: None), __exit__=lambda *a: None),
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
hardware_exceptions = types.ModuleType("hardware.exceptions")
hardware_gpio_types.DigitalInput = object
hardware_gpio_types.DigitalOutput = object
hardware_gpio_types.GpioEvent = object
hardware_gpio_types.GpioSubscription = object
hardware_exceptions.MotorError = Exception
hardware_gpio.types = hardware_gpio_types

hardware_module.controller = hardware_controller
hardware_module.motor = hardware_motor
hardware_module.servo = hardware_servo
hardware_module.curtain = hardware_curtain
hardware_module.scale = hardware_scale
hardware_module.toolcam = hardware_toolcam
hardware_module.laser = hardware_laser
hardware_module.gpio = hardware_gpio
hardware_module.exceptions = hardware_exceptions

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
sys.modules.setdefault("hardware.exceptions", hardware_exceptions)


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
        self.restore_y_brake_output_calls = 0
        self.handled_commands = []
        self.shutdown_calls = 0
        self.lifecycle_events = []

    def block_motion_for_safety(self):
        self.safety_paused = True

    def release_motion_safety(self):
        self.safety_paused = False

    async def get_status(self):
        self.lifecycle_events.append("status")
        if self.shutdown_calls > 0:
            raise RuntimeError("GPIO is closed")
        return None

    async def restore_y_brake_output(self):
        self.restore_y_brake_output_calls += 1

    async def handle_command(self, command):
        self.handled_commands.append(command)
        return None

    async def shutdown(self):
        self.lifecycle_events.append("shutdown")
        self.shutdown_calls += 1

    def clear_operation_context(self):
        pass

    def register_light_curtain_listener(self, cb):
        pass

    def register_cabinet_door_listener(self, cb):
        pass


class FakeEstopInput:
    def __init__(self):
        self.value = False

    def read(self):
        return self.value


class FakeEstopSubscription:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class SubscribableEstopInput(FakeEstopInput):
    def __init__(self):
        super().__init__()
        self.callback = None
        self.subscription = FakeEstopSubscription()

    def subscribe(self, callback):
        self.callback = callback
        return self.subscription

    def set_value(self, value, *, notify=True):
        self.value = value
        if notify and self.callback is not None:
            self.callback(types.SimpleNamespace(value=value))


class EstopGpio:
    def __init__(self, estop_input):
        self.estop_input = estop_input

    def has_pin(self, name):
        return name == "estop"

    def input(self, name):
        assert name == "estop"
        return self.estop_input


class FailingEstopInput:
    def read(self):
        raise RuntimeError("i2c unavailable")


class FailingGpio:
    def has_pin(self, name):
        return name == "estop"

    def input(self, name):
        raise RuntimeError(f"{name} unavailable")


class EmergencyStopConfigWrapper:
    def __init__(self, enabled):
        self._enabled = enabled

    def get(self, path, default=None):
        if path == "robot.emergency_stop":
            return {"enabled": self._enabled}
        return default


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
        self.assertTrue(worker.needs_home())
        self.assertEqual(worker.needs_home_reason(), "test reason")
        self.assertTrue(robot.safety_paused)

        payload = json.loads(await worker.get_status_snapshot(force_refresh=True))
        self.assertTrue(payload["is_emergency_stopped"])
        self.assertFalse(payload["is_emergency_stop_active"])
        self.assertEqual(payload["emergency_stop_reason"], "test reason")
        self.assertTrue(payload["needs_home"])
        self.assertEqual(payload["needs_home_reason"], "test reason")

        RobotWorker._instance = None

    async def test_successful_home_clears_emergency_stop_home_requirement(self):
        from openshelf_os.robot.commands import HomeCommand
        from openshelf_os.robot.status import RobotCommandSuccessful
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        await worker.emergency_stop("test reason")

        async def successful_home():
            return RobotCommandSuccessful(command="home")

        task = asyncio.create_task(successful_home())
        await task
        await worker._publish_completed_command_result(task, HomeCommand())

        self.assertFalse(worker._is_emergency_stopped)
        self.assertFalse(worker.needs_home())
        self.assertFalse(robot.safety_paused)

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

    async def test_emergency_stop_cleanup_restores_y_brake_output(self):
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)

        await worker.emergency_stop("test")
        await asyncio.sleep(0.1)

        self.assertEqual(robot.restore_y_brake_output_calls, 1)

        RobotWorker._instance = None

    async def test_emergency_cleanup_preserves_command_admitted_during_brake_restore(self):
        from openshelf_os.robot.commands import RetrieveItemCommand
        from openshelf_os.robot.status import RobotCommandSuccessful
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        events = await worker.subscribe_events()
        brake_restore_started = asyncio.Event()
        release_brake_restore = asyncio.Event()
        cleanup_complete = asyncio.Event()
        process_task = None

        async def gated_brake_restore():
            robot.restore_y_brake_output_calls += 1
            brake_restore_started.set()
            await release_brake_restore.wait()

        original_discard_queued_commands = worker._discard_queued_commands

        async def track_cleanup_discard(reason, *, interruption_boundary=None):
            await original_discard_queued_commands(
                reason,
                interruption_boundary=interruption_boundary,
            )
            cleanup_complete.set()

        async def no_safety_change(*, refresh=True):
            return False

        async def successful_handler(command):
            robot.handled_commands.append(command)
            return RobotCommandSuccessful(command=command.command, upca=command.upca)

        queued_before_stop = RetrieveItemCommand(
            request_id="bin-workflow-before-emergency-boundary",
            upca="900000000013",
            location={"side": "left_module", "shelf_idx": 2, "cell_idx": 4},
        )
        recovered_command = RetrieveItemCommand(
            request_id="bin-workflow-after-emergency-boundary",
            upca="900000000014",
            location={"side": "right_module", "shelf_idx": 1, "cell_idx": 3},
        )
        robot.restore_y_brake_output = gated_brake_restore
        robot.handle_command = successful_handler
        worker._discard_queued_commands = track_cleanup_discard
        worker._evaluate_safety_guard = no_safety_change
        await worker.enqueue_command(queued_before_stop)

        try:
            await worker.emergency_stop("brake restore boundary")
            await asyncio.wait_for(brake_restore_started.wait(), timeout=1)

            await worker.enqueue_command(recovered_command)
            release_brake_restore.set()
            await asyncio.wait_for(cleanup_complete.wait(), timeout=1)

            failed_event = {}
            for _ in range(3):
                failed_event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
                if failed_event.get("request_id") == queued_before_stop.request_id:
                    break
            assert_correlated_failure(self, failed_event, {
                "command": "retrieve_item",
                "message": "error",
                "error": "Emergency stop: brake restore boundary",
                "request_id": "bin-workflow-before-emergency-boundary",
                "upca": "900000000013",
                "location": {"side": "left_module", "shelf_idx": 2, "cell_idx": 4},
            })
            remaining_events = []
            while events.empty() is False:
                remaining_events.append(json.loads(events.get_nowait()))
            self.assertFalse(any(
                event.get("request_id") == recovered_command.request_id
                and event.get("message") == "failed"
                for event in remaining_events
            ))
            self.assertEqual(
                [operation.command for operation in worker._queued_commands],
                [recovered_command],
            )
            self.assertEqual(worker._command_queue.qsize(), 1)

            worker._running = True
            process_task = asyncio.create_task(worker.process_commands())
            success_event = {}
            for _ in range(3):
                success_event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
                if success_event.get("request_id") == recovered_command.request_id:
                    break
            assert_correlated_success(self, success_event, {
                "command": "retrieve_item",
                "message": "success",
                "location": None,
                "upca": "900000000014",
                "weight": None,
                "request_id": "bin-workflow-after-emergency-boundary",
            })
            self.assertEqual(robot.handled_commands, [recovered_command])
        finally:
            worker._running = False
            release_brake_restore.set()
            if process_task is not None and process_task.done() is False:
                process_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await process_task
            RobotWorker._instance = None

    async def test_emergency_stop_cleanup_restores_y_brake_output_when_cancelled_task_fails(self):
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None

        worker = RobotWorker(robot)
        task_cancelled = asyncio.Event()

        async def failing_command():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                task_cancelled.set()
                raise RuntimeError("cleanup failed")

        worker._current_task = asyncio.create_task(failing_command())

        await worker.emergency_stop("test")
        await asyncio.sleep(0.1)

        self.assertTrue(task_cancelled.is_set())
        self.assertEqual(robot.restore_y_brake_output_calls, 1)
        self.assertIsNone(worker._current_task)

        RobotWorker._instance = None

    async def test_emergency_stop_preserves_completed_retrieve_success_once(self):
        from openshelf_os.robot.commands import RetrieveItemCommand
        from openshelf_os.robot.status import RobotCommandSuccessful
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        events = await worker.subscribe_events()

        async def completed_command():
            return RobotCommandSuccessful(
                command="retrieve_item",
                upca="900000000005",
                weight=123.0,
            )

        worker._current_command = RetrieveItemCommand(
            request_id="bin-workflow-completed-stop",
            upca="900000000005",
            location={"side": "left_module", "shelf_idx": 1, "cell_idx": 2},
        )
        worker._current_operation = worker._snapshot_command(worker._current_command)
        worker._queued_commands.append(worker._current_operation)
        worker._current_task = asyncio.create_task(completed_command())
        await asyncio.sleep(0)

        async def no_status_publish():
            return None

        async def no_safety_change(*, refresh=True):
            return False

        worker._publish_status = no_status_publish
        worker._evaluate_safety_guard = no_safety_change
        worker._running = True
        process_task = asyncio.create_task(worker.process_commands())

        try:
            await worker.emergency_stop("test")

            event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
            assert_correlated_success(self, event, {
                "command": "retrieve_item",
                "message": "success",
                "location": None,
                "upca": "900000000005",
                "weight": 123.0,
                "request_id": "bin-workflow-completed-stop",
            })
            await asyncio.sleep(0.05)
            self.assertTrue(events.empty())
            self.assertIsNone(worker._current_task)
            self.assertEqual(worker._queued_commands, [])
        finally:
            worker._running = False
            process_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await process_task
            RobotWorker._instance = None

    async def test_emergency_stop_fails_each_queued_storage_command(self):
        from openshelf_os.robot.commands import RetrieveItemCommand, StoreItemCommand
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        events = await worker.subscribe_events()
        retrieve = RetrieveItemCommand(
            request_id="bin-workflow-queued-retrieve",
            upca="900000000006",
            location={"side": "right_module", "shelf_idx": 2, "cell_idx": 3},
        )
        store = StoreItemCommand(
            request_id="bin-workflow-queued-store",
            item={
                "upca": "900000000007",
                "location": {"side": "left_module", "shelf_idx": 4, "cell_idx": 5},
            },
        )
        await worker.enqueue_command(retrieve)
        await worker.enqueue_command(store)

        await worker.emergency_stop("test")

        failures = {}
        for _ in range(4):
            event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
            if event.get("command") in {"retrieve_item", "store_item"}:
                failures[event["request_id"]] = event
            if len(failures) == 2:
                break
        expected_failures = {
            "bin-workflow-queued-retrieve": {
                "command": "retrieve_item",
                "message": "error",
                "error": "Emergency stop: test",
                "request_id": "bin-workflow-queued-retrieve",
                "upca": "900000000006",
                "location": {"side": "right_module", "shelf_idx": 2, "cell_idx": 3},
            },
            "bin-workflow-queued-store": {
                "command": "store_item",
                "message": "error",
                "error": "Emergency stop: test",
                "request_id": "bin-workflow-queued-store",
                "upca": "900000000007",
                "location": {"side": "left_module", "shelf_idx": 4, "cell_idx": 5},
            },
        }
        for request_id, expected in expected_failures.items():
            assert_correlated_failure(self, failures[request_id], expected)
        self.assertEqual(worker._queued_commands, [])
        self.assertTrue(worker._command_queue.empty())

        RobotWorker._instance = None

    async def test_emergency_stop_publishes_correlated_failure_for_cancelled_retrieve(self):
        from openshelf_os.robot.commands import RetrieveItemCommand
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        events = await worker.subscribe_events()

        async def slow_command():
            await asyncio.sleep(10)

        worker._current_command = RetrieveItemCommand(
            request_id="bin-workflow-1",
            upca="900000000001",
            location={"side": "left_module", "shelf_idx": 1, "cell_idx": 2},
        )
        worker._current_operation = worker._snapshot_command(worker._current_command)
        worker._current_task = asyncio.create_task(slow_command())

        await worker.emergency_stop("test")

        event = {}
        for _ in range(2):
            event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
            if event.get("command") == "retrieve_item":
                break
        assert_correlated_failure(self, event, {
            "command": "retrieve_item",
            "message": "error",
            "error": "Emergency stop: test",
            "request_id": "bin-workflow-1",
            "upca": "900000000001",
            "location": {"side": "left_module", "shelf_idx": 1, "cell_idx": 2},
        })

        RobotWorker._instance = None

    async def test_worker_publishes_correlated_failure_when_retrieve_faults(self):
        from openshelf_os.exceptions import MotorError
        from openshelf_os.robot.commands import RetrieveItemCommand
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        events = await worker.subscribe_events()

        async def faulting_command():
            raise MotorError("motor fault")

        async def no_status_publish():
            return None

        async def no_safety_change(*, refresh=True):
            return False

        worker._current_command = RetrieveItemCommand(
            request_id="bin-workflow-2",
            upca="900000000002",
            location={"side": "right_module", "shelf_idx": 3, "cell_idx": 4},
        )
        worker._current_operation = worker._snapshot_command(worker._current_command)
        worker._current_task = asyncio.create_task(faulting_command())
        worker._publish_status = no_status_publish
        worker._evaluate_safety_guard = no_safety_change
        worker._running = True
        process_task = asyncio.create_task(worker.process_commands())

        try:
            event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
            assert_correlated_failure(self, event, {
                "command": "retrieve_item",
                "message": "error",
                "error": "motor fault",
                "request_id": "bin-workflow-2",
                "upca": "900000000002",
                "location": {"side": "right_module", "shelf_idx": 3, "cell_idx": 4},
            }, code="execution_error")
        finally:
            worker._running = False
            process_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await process_task
            RobotWorker._instance = None

    async def test_worker_publishes_correlated_success_for_retrieve(self):
        from openshelf_os.robot.commands import RetrieveItemCommand
        from openshelf_os.robot.status import RobotCommandSuccessful
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        events = await worker.subscribe_events()

        async def successful_command():
            return RobotCommandSuccessful(
                command="retrieve_item",
                upca="900000000003",
                weight=123.0,
            )

        async def no_status_publish():
            return None

        async def no_safety_change(*, refresh=True):
            return False

        worker._current_command = RetrieveItemCommand(
            request_id="bin-workflow-3",
            upca="900000000003",
            location={"side": "left_module", "shelf_idx": 1, "cell_idx": 2},
        )
        worker._current_operation = worker._snapshot_command(worker._current_command)
        worker._current_task = asyncio.create_task(successful_command())
        worker._publish_status = no_status_publish
        worker._evaluate_safety_guard = no_safety_change
        worker._running = True
        process_task = asyncio.create_task(worker.process_commands())

        try:
            event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
            assert_correlated_success(self, event, {
                "command": "retrieve_item",
                "message": "success",
                "location": None,
                "upca": "900000000003",
                "weight": 123.0,
                "request_id": "bin-workflow-3",
            })
        finally:
            worker._running = False
            process_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await process_task
            RobotWorker._instance = None

    async def test_worker_preserves_non_storage_error_event_publication(self):
        from openshelf_os.robot.commands import HomeCommand
        from openshelf_os.robot.status import RobotCommandError
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        events = await worker.subscribe_events()

        async def failed_home_command():
            return RobotCommandError(command="home", error="home failed")

        async def no_status_publish():
            return None

        async def no_safety_change(*, refresh=True):
            return False

        worker._current_command = HomeCommand()
        worker._current_operation = worker._snapshot_command(worker._current_command)
        worker._current_task = asyncio.create_task(failed_home_command())
        worker._publish_status = no_status_publish
        worker._evaluate_safety_guard = no_safety_change
        worker._running = True
        process_task = asyncio.create_task(worker.process_commands())

        try:
            event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
            self.assertEqual(event["command"], "home")
            self.assertEqual(event["message"], "failed")
            self.assertEqual(event["error"], "home failed")
            self.assertEqual(event["code"], "execution_failed")
            uuid.UUID(event["command_id"])
        finally:
            worker._running = False
            process_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await process_task
            RobotWorker._instance = None

    async def test_emergency_stop_fails_dequeued_storage_command_before_task_creation(self):
        from openshelf_os.robot.commands import RetrieveItemCommand
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        events = await worker.subscribe_events()
        status_blocked = asyncio.Event()
        release_status = asyncio.Event()
        process_task = None

        async def block_dequeue_status():
            if asyncio.current_task() is process_task:
                status_blocked.set()
                await release_status.wait()

        async def no_safety_change(*, refresh=True):
            return False

        command = RetrieveItemCommand(
            request_id="bin-workflow-dequeued-estop",
            upca="900000000009",
            location={"side": "right_module", "shelf_idx": 2, "cell_idx": 4},
        )
        await worker.enqueue_command(command)
        worker._publish_status = block_dequeue_status
        worker._evaluate_safety_guard = no_safety_change
        worker._running = True
        process_task = asyncio.create_task(worker.process_commands())

        try:
            await asyncio.wait_for(status_blocked.wait(), timeout=1)
            await worker.emergency_stop("dequeue window")

            event = {}
            for _ in range(3):
                event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
                if event.get("request_id") == command.request_id:
                    break
            assert_correlated_failure(self, event, {
                "command": "retrieve_item",
                "message": "error",
                "error": "Emergency stop: dequeue window",
                "request_id": "bin-workflow-dequeued-estop",
                "upca": "900000000009",
                "location": {"side": "right_module", "shelf_idx": 2, "cell_idx": 4},
            })
            await asyncio.sleep(0)
            remaining_events = []
            while events.empty() is False:
                remaining_events.append(json.loads(events.get_nowait()))
            self.assertFalse(any(
                queued_event.get("request_id") == command.request_id
                for queued_event in remaining_events
            ))
            self.assertIsNone(worker._current_command)
            self.assertIsNone(worker._current_task)

            release_status.set()
            await asyncio.sleep(0.05)
            self.assertEqual(robot.handled_commands, [])
        finally:
            worker._running = False
            release_status.set()
            process_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await process_task
            RobotWorker._instance = None

    async def test_clear_fails_dequeued_command_before_admission(self):
        from openshelf_os.robot.commands import RetrieveItemCommand
        from openshelf_os.robot.worker import RobotWorker

        class DequeueGate(asyncio.Queue):
            def __init__(self):
                super().__init__()
                self.dequeued = asyncio.Event()
                self.release_process = asyncio.Event()

            async def get(self):
                operation = await super().get()
                self.dequeued.set()
                await self.release_process.wait()
                return operation

        robot = MockRobot()
        lifecycle_events = []
        clear_terminalization_started = asyncio.Event()
        release_clear_terminalization = asyncio.Event()

        async def lifecycle_callback(status, operation, payload):
            lifecycle_events.append((status, operation, payload))
            clear_terminalization_started.set()
            await release_clear_terminalization.wait()

        RobotWorker._instance = None
        worker = RobotWorker(robot, command_lifecycle_callback=lifecycle_callback)
        events = await worker.subscribe_events()
        queue = DequeueGate()
        worker._command_queue = queue
        process_task = None

        async def no_status_publish():
            return None

        async def no_safety_change(*, refresh=True):
            return False

        command = RetrieveItemCommand(
            request_id="bin-workflow-dequeued-clear",
            upca="900000000011",
            location={"side": "right_module", "shelf_idx": 4, "cell_idx": 1},
        )
        worker._publish_status = no_status_publish
        worker._evaluate_safety_guard = no_safety_change
        await worker.enqueue_command(command)
        worker._running = True
        process_task = asyncio.create_task(worker.process_commands())

        try:
            await asyncio.wait_for(queue.dequeued.wait(), timeout=1)
            clear_task = asyncio.create_task(worker.clear_commands())
            await asyncio.wait_for(clear_terminalization_started.wait(), timeout=1)

            queue.release_process.set()
            await asyncio.sleep(0)
            self.assertIsNone(worker._current_operation)

            release_clear_terminalization.set()
            await clear_task

            event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
            assert_correlated_failure(self, event, {
                "command": "retrieve_item",
                "message": "error",
                "error": "Commands cleared",
                "request_id": "bin-workflow-dequeued-clear",
                "upca": "900000000011",
                "location": {"side": "right_module", "shelf_idx": 4, "cell_idx": 1},
            })
            await asyncio.sleep(0.05)

            self.assertEqual(robot.handled_commands, [])
            self.assertTrue(events.empty())
            self.assertEqual(worker._queued_commands, [])
            self.assertTrue(worker._command_queue.empty())
            self.assertIsNone(worker._current_command)
            self.assertIsNone(worker._current_task)
            self.assertEqual(len(lifecycle_events), 1)
            self.assertEqual(lifecycle_events[0][0], "failed")
            self.assertEqual(lifecycle_events[0][1].command_id, event["command_id"])
        finally:
            worker._running = False
            queue.release_process.set()
            release_clear_terminalization.set()
            if process_task is not None and process_task.done() is False:
                process_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await process_task
            RobotWorker._instance = None

    async def test_clear_allows_command_enqueued_after_idle_waiter(self):
        from openshelf_os.robot.commands import RetrieveItemCommand
        from openshelf_os.robot.status import RobotCommandSuccessful
        from openshelf_os.robot.worker import RobotWorker

        class IdleWaiterQueue(asyncio.Queue):
            def __init__(self):
                super().__init__()
                self.waiting = asyncio.Event()

            async def get(self):
                self.waiting.set()
                return await super().get()

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        events = await worker.subscribe_events()
        worker._command_queue = IdleWaiterQueue()
        process_task = None

        async def no_status_publish():
            return None

        async def no_safety_change(*, refresh=True):
            return False

        async def successful_handler(command):
            robot.handled_commands.append(command)
            return RobotCommandSuccessful(command=command.command, upca=command.upca)

        worker._publish_status = no_status_publish
        worker._evaluate_safety_guard = no_safety_change
        robot.handle_command = successful_handler
        worker._running = True
        process_task = asyncio.create_task(worker.process_commands())

        try:
            await asyncio.wait_for(worker._command_queue.waiting.wait(), timeout=1)
            await worker.clear_commands()

            command = RetrieveItemCommand(
                request_id="bin-workflow-after-idle-clear",
                upca="900000000012",
                location={"side": "left_module", "shelf_idx": 2, "cell_idx": 3},
            )
            await worker.enqueue_command(command)

            event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
            assert_correlated_success(self, event, {
                "command": "retrieve_item",
                "message": "success",
                "location": None,
                "upca": "900000000012",
                "weight": None,
                "request_id": "bin-workflow-after-idle-clear",
            })
            self.assertEqual(robot.handled_commands, [command])
        finally:
            worker._running = False
            if process_task is not None and process_task.done() is False:
                process_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await process_task
            RobotWorker._instance = None

    async def test_shutdown_fails_dequeued_storage_command_before_task_creation(self):
        from openshelf_os.robot.commands import RetrieveItemCommand
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        events = await worker.subscribe_events()
        status_blocked = asyncio.Event()
        release_status = asyncio.Event()
        process_task = None

        async def block_dequeue_status():
            if asyncio.current_task() is process_task:
                status_blocked.set()
                await release_status.wait()

        async def no_safety_change(*, refresh=True):
            return False

        command = RetrieveItemCommand(
            request_id="bin-workflow-dequeued-shutdown",
            upca="900000000010",
            location={"side": "left_module", "shelf_idx": 3, "cell_idx": 1},
        )
        await worker.enqueue_command(command)
        worker._publish_status = block_dequeue_status
        worker._evaluate_safety_guard = no_safety_change
        worker._running = True
        process_task = asyncio.create_task(worker.process_commands())
        worker._worker_task = process_task

        try:
            await asyncio.wait_for(status_blocked.wait(), timeout=1)
            await worker.shutdown()

            event = {}
            for _ in range(3):
                event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
                if event.get("request_id") == command.request_id:
                    break
            assert_correlated_failure(self, event, {
                "command": "retrieve_item",
                "message": "error",
                "error": "Robot stopped during shutdown",
                "request_id": "bin-workflow-dequeued-shutdown",
                "upca": "900000000010",
                "location": {"side": "left_module", "shelf_idx": 3, "cell_idx": 1},
            })
            remaining_events = []
            while events.empty() is False:
                remaining_events.append(json.loads(events.get_nowait()))
            self.assertFalse(any(
                queued_event.get("request_id") == command.request_id
                for queued_event in remaining_events
            ))
            self.assertIsNone(worker._current_command)
            self.assertIsNone(worker._current_task)
            self.assertEqual([command.command for command in robot.handled_commands], ["stop"])
        finally:
            release_status.set()
            if process_task.done() is False:
                process_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await process_task
            RobotWorker._instance = None

    async def test_shutdown_cancels_running_command_task(self):
        from openshelf_os.robot.commands import RetrieveItemCommand
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        task_cancelled = asyncio.Event()
        events = await worker.subscribe_events()

        async def slow_command():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                task_cancelled.set()
                raise

        worker._running = True
        worker._current_command = RetrieveItemCommand(
            request_id="bin-workflow-shutdown",
            upca="900000000004",
            location={"side": "left_module", "shelf_idx": 1, "cell_idx": 2},
        )
        worker._current_operation = worker._snapshot_command(worker._current_command)
        worker._current_task = asyncio.create_task(slow_command())
        await asyncio.sleep(0)

        await worker.shutdown()

        event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
        self.assertTrue(task_cancelled.is_set())
        self.assertIsNone(worker._current_task)
        assert_correlated_failure(self, event, {
            "command": "retrieve_item",
            "message": "error",
            "error": "Robot stopped during shutdown",
            "request_id": "bin-workflow-shutdown",
            "upca": "900000000004",
            "location": {"side": "left_module", "shelf_idx": 1, "cell_idx": 2},
        })
        self.assertEqual(robot.shutdown_calls, 1)
        self.assertEqual(robot.lifecycle_events[-2:], ["status", "shutdown"])
        self.assertFalse(any(error.error_type == "StatusRetrievalError" for error in worker._error_queue))

        RobotWorker._instance = None

    async def test_shutdown_preserves_completed_retrieve_success(self):
        from openshelf_os.robot.commands import RetrieveItemCommand
        from openshelf_os.robot.status import RobotCommandSuccessful
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        events = await worker.subscribe_events()

        async def completed_command():
            return RobotCommandSuccessful(
                command="retrieve_item",
                upca="900000000008",
                weight=456.0,
            )

        worker._current_command = RetrieveItemCommand(
            request_id="bin-workflow-completed-shutdown",
            upca="900000000008",
            location={"side": "left_module", "shelf_idx": 1, "cell_idx": 2},
        )
        worker._current_operation = worker._snapshot_command(worker._current_command)
        worker._queued_commands.append(worker._current_operation)
        worker._current_task = asyncio.create_task(completed_command())
        await asyncio.sleep(0)

        await worker.shutdown()

        event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
        assert_correlated_success(self, event, {
            "command": "retrieve_item",
            "message": "success",
            "location": None,
            "upca": "900000000008",
            "weight": 456.0,
            "request_id": "bin-workflow-completed-shutdown",
        })
        self.assertIsNone(worker._current_task)
        self.assertEqual(worker._queued_commands, [])

        RobotWorker._instance = None

    async def test_estop_monitor_skips_when_disabled_in_config(self):
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        robot._gpio = FailingGpio()
        robot._config = EmergencyStopConfigWrapper(False)
        RobotWorker._instance = None
        worker = RobotWorker(robot)

        await worker._start_estop_monitor()

        self.assertFalse(worker._is_emergency_stopped)
        self.assertFalse(worker._is_estop_active)
        self.assertFalse(robot.safety_paused)
        self.assertIsNone(worker._estop_monitor_task)

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

    async def test_estop_interrupt_wakes_continuous_monitor_immediately(self):
        import openshelf_os.robot.worker as worker_module

        estop_input = SubscribableEstopInput()
        robot = MockRobot()
        robot._gpio = EstopGpio(estop_input)
        RobotWorker = worker_module.RobotWorker
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        worker._running = True

        try:
            with patch.object(worker_module, "ESTOP_POLL_INTERVAL_SECONDS", 10.0):
                await worker._start_estop_monitor()
                await asyncio.sleep(0)

                self.assertIsNotNone(worker._estop_subscription)
                self.assertIsNotNone(worker._estop_monitor_task)

                estop_input.set_value(True)
                for _ in range(20):
                    if worker._is_estop_active is True:
                        break
                    await asyncio.sleep(0.005)

                self.assertTrue(worker._is_estop_active)
                self.assertTrue(worker._is_emergency_stopped)
                self.assertEqual(worker._emergency_stop_reason, "Physical emergency stop triggered")
        finally:
            worker._running = False
            await worker._stop_estop_monitor()
            self.assertTrue(estop_input.subscription.closed)
            RobotWorker._instance = None

    async def test_estop_polling_detects_change_when_subscription_is_silent(self):
        import openshelf_os.robot.worker as worker_module

        estop_input = SubscribableEstopInput()
        robot = MockRobot()
        robot._gpio = EstopGpio(estop_input)
        RobotWorker = worker_module.RobotWorker
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        worker._running = True

        try:
            with patch.object(worker_module, "ESTOP_POLL_INTERVAL_SECONDS", 0.005):
                await worker._start_estop_monitor()
                await asyncio.sleep(0)

                estop_input.set_value(True, notify=False)
                await asyncio.sleep(0.02)

                self.assertTrue(worker._is_estop_active)
                self.assertTrue(worker._is_emergency_stopped)
                self.assertEqual(worker._emergency_stop_reason, "Physical emergency stop triggered")
        finally:
            worker._running = False
            await worker._stop_estop_monitor()
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
