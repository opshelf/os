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

    def register_weight_change_listener(self, cb):
        pass

    async def start_weight_guard(self):
        pass

    async def stop_weight_guard(self):
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


    async def test_emergency_stop_fails_each_queued_storage_command(self):
        from openshelf_os.robot.commands import RetrieveItemCommand, StoreItemCommand
        from openshelf_os.robot.worker import RobotWorker

        robot = MockRobot()
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        events = await worker.subscribe_events()
        retrieve = RetrieveItemCommand(
            request_id="bin-workflow-queued-retrieve",
            barcode="900000000006",
            location={"side": "back_module", "shelf_idx": 2, "cell_idx": 3},
        )
        store = StoreItemCommand(
            request_id="bin-workflow-queued-store",
            item={
                "barcode": "900000000007",
                "location": {"side": "front_module", "shelf_idx": 4, "cell_idx": 5},
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
                "barcode": "900000000006",
                "location": {"side": "back_module", "shelf_idx": 2, "cell_idx": 3},
            },
            "bin-workflow-queued-store": {
                "command": "store_item",
                "message": "error",
                "error": "Emergency stop: test",
                "request_id": "bin-workflow-queued-store",
                "barcode": "900000000007",
                "location": {"side": "front_module", "shelf_idx": 4, "cell_idx": 5},
            },
        }
        for request_id, expected in expected_failures.items():
            assert_correlated_failure(self, failures[request_id], expected)
        self.assertEqual(worker._queued_commands, [])
        self.assertTrue(worker._command_queue.empty())

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
