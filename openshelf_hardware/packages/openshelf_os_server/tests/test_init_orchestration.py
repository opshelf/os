from dataclasses import replace
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import openshelf_os_server.core.robot as robot_module
import openshelf_os_server.main as main_module
from openshelf_os_server.core.init_progress import OVERALL_READY, get_init_tracker
from openshelf_os_server.core.printer_status import PrinterStatus, PrinterStatusProbeError
from openshelf_os.operating_mode import OperatingMode


def _phase_for(component: str) -> str | None:
    snapshot = get_init_tracker().snapshot()
    for entry in snapshot["components"]:
        if entry["component"] == component:
            return entry["phase"]
    return None


def _reason_for(component: str) -> str | None:
    snapshot = get_init_tracker().snapshot()
    for entry in snapshot["components"]:
        if entry["component"] == component:
            return entry["reason"]
    return None


def test_barcode_enabled_honors_env(monkeypatch):
    monkeypatch.delenv("OPENSHELF_DISABLE_BARCODE", raising=False)
    assert robot_module._barcode_enabled() is True

    monkeypatch.setenv("OPENSHELF_DISABLE_BARCODE", "true")
    assert robot_module._barcode_enabled() is False


@pytest.mark.anyio
async def test_report_scanner_status_ready_when_connected(monkeypatch):
    monkeypatch.delenv("OPENSHELF_DISABLE_BARCODE", raising=False)
    get_init_tracker().reset()
    worker = MagicMock()
    worker.wait_scanner_connected = AsyncMock(return_value=True)

    await robot_module._report_scanner_status(worker)

    assert _phase_for("barcode") == "ready"


@pytest.mark.anyio
async def test_report_scanner_status_failed_when_not_detected(monkeypatch):
    monkeypatch.delenv("OPENSHELF_DISABLE_BARCODE", raising=False)
    get_init_tracker().reset()
    worker = MagicMock()
    worker.wait_scanner_connected = AsyncMock(return_value=False)
    worker.describe_scanner_failure = MagicMock(return_value="scanner not detected")

    await robot_module._report_scanner_status(worker)

    assert _phase_for("barcode") == "failed"
    assert _reason_for("barcode") == "scanner not detected"


@pytest.mark.anyio
async def test_report_scanner_status_failed_reports_hid_mode(monkeypatch):
    monkeypatch.delenv("OPENSHELF_DISABLE_BARCODE", raising=False)
    get_init_tracker().reset()
    worker = MagicMock()
    worker.wait_scanner_connected = AsyncMock(return_value=False)
    worker.describe_scanner_failure = MagicMock(
        return_value="scanner is in usb hid keyboard mode; switch it to usb-com (serial) mode"
    )

    await robot_module._report_scanner_status(worker)

    assert _phase_for("barcode") == "failed"
    assert _reason_for("barcode") == (
        "scanner is in usb hid keyboard mode; switch it to usb-com (serial) mode"
    )


@pytest.mark.anyio
async def test_report_scanner_status_skipped_when_disabled(monkeypatch):
    monkeypatch.setenv("OPENSHELF_DISABLE_BARCODE", "true")
    get_init_tracker().reset()
    worker = MagicMock()
    worker.wait_scanner_connected = AsyncMock(return_value=True)

    await robot_module._report_scanner_status(worker)

    assert _phase_for("barcode") is None
    worker.wait_scanner_connected.assert_not_called()


def test_robot_health_is_ok_when_only_camera_component_failed(monkeypatch):
    tracker = get_init_tracker()
    tracker.reset()
    tracker.register_components(["command_worker"])
    tracker.report("command_worker", "ready")
    tracker.register_components(["tool_camera"], degrade=False)
    tracker.set_overall(OVERALL_READY)
    tracker.report("tool_camera", "failed", reason="no frames within 15s")

    monkeypatch.setattr(robot_module, "robot_worker", MagicMock())

    health = robot_module.get_robot_health()

    assert health["status"] == "ok"
    assert health["phase"] == OVERALL_READY
    assert health["failed_components"] == []


def test_robot_health_is_degraded_when_motor_component_failed(monkeypatch):
    tracker = get_init_tracker()
    tracker.reset()
    tracker.register_components(["command_worker", "y_axis"])
    tracker.report("command_worker", "ready")
    tracker.report("y_axis", "failed", reason="fault")
    tracker.set_overall("degraded")

    monkeypatch.setattr(robot_module, "robot_worker", MagicMock())

    health = robot_module.get_robot_health()

    assert health["status"] == "degraded"
    assert health["failed_components"] == ["y_axis"]


def _printer_status(**overrides: bool) -> PrinterStatus:
    status = PrinterStatus(
        raw=(0x12, 0x12, 0x12, 0x12),
        offline=False,
        waiting_for_online_recovery=False,
        feed_button_active=False,
        cover_open=False,
        paper_out=False,
        paper_near_end=False,
        recoverable_error=False,
        cutter_error=False,
        unrecoverable_error=False,
        auto_recoverable_error=False,
    )
    return replace(status, **overrides)


def test_report_printer_status_failed_when_probe_fails(monkeypatch):
    get_init_tracker().reset()
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: True)
    monkeypatch.setattr(
        robot_module,
        "probe_printer_status",
        MagicMock(side_effect=PrinterStatusProbeError("device not found: /dev/usb/lp0")),
    )

    robot_module._report_printer_status()

    assert _phase_for("printer") == "failed"
    assert _reason_for("printer") == "device not found: /dev/usb/lp0"


def test_report_printer_status_ready_when_probe_is_healthy(monkeypatch):
    get_init_tracker().reset()
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: True)
    probe = MagicMock(return_value=_printer_status())
    monkeypatch.setattr(robot_module, "probe_printer_status", probe)

    robot_module._report_printer_status()

    assert _phase_for("printer") == "ready"
    probe.assert_called_once_with("/dev/usb/lp0")


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ({"paper_out": True, "offline": True}, "paper out"),
        ({"cover_open": True, "offline": True}, "cover open"),
        ({"cutter_error": True, "offline": True}, "cutter error"),
        ({"recoverable_error": True, "offline": True}, "recoverable printer error"),
        ({"unrecoverable_error": True, "offline": True}, "unrecoverable printer error"),
        ({"auto_recoverable_error": True, "offline": True}, "auto-recoverable printer error"),
        ({"offline": True}, "printer offline"),
    ],
)
def test_report_printer_status_failed_for_unhealthy_status(monkeypatch, condition, reason):
    get_init_tracker().reset()
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: True)
    monkeypatch.setattr(robot_module, "probe_printer_status", MagicMock(return_value=_printer_status(**condition)))

    robot_module._report_printer_status()

    assert _phase_for("printer") == "failed"
    assert _reason_for("printer") == reason


def test_report_printer_status_ready_with_near_end_detail(monkeypatch):
    get_init_tracker().reset()
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: True)
    monkeypatch.setattr(
        robot_module,
        "probe_printer_status",
        MagicMock(return_value=_printer_status(paper_near_end=True)),
    )

    robot_module._report_printer_status()

    snapshot = get_init_tracker().snapshot()
    printer = next(entry for entry in snapshot["components"] if entry["component"] == "printer")
    assert printer["phase"] == "ready"
    assert printer["detail"] == "/dev/usb/lp0 (paper near end)"


def test_report_printer_status_skipped_when_disabled(monkeypatch):
    get_init_tracker().reset()
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: False)

    robot_module._report_printer_status()

    assert _phase_for("printer") is None


@pytest.mark.anyio
async def test_stop_robot_worker_drains_terminal_events_before_stopping_listener(monkeypatch) -> None:
    events: list[str] = []

    class Worker:
        async def shutdown(self) -> None:
            events.append("worker_shutdown")

    class Listener:
        async def drain(self) -> None:
            assert events == ["worker_shutdown"]
            events.append("listener_drain")

        async def stop(self) -> None:
            assert events == ["worker_shutdown", "listener_drain"]
            events.append("listener_stop")

    worker = Worker()
    listener = Listener()
    monkeypatch.setattr(robot_module, "robot_worker", worker)
    monkeypatch.setattr(robot_module, "robot_instance", object())
    monkeypatch.setattr(robot_module, "event_listener", listener)

    await robot_module.stop_robot_worker()

    assert events == ["worker_shutdown", "listener_drain", "listener_stop"]
    assert robot_module.robot_worker is None
    assert robot_module.robot_instance is None
    assert robot_module.event_listener is None


@pytest.mark.anyio
async def test_stop_robot_worker_cleans_partial_listener_after_worker_failure(monkeypatch) -> None:
    events: list[str] = []

    class Worker:
        async def shutdown(self) -> None:
            events.append("worker_shutdown")
            raise RuntimeError("shutdown failed")

    class Listener:
        async def drain(self) -> None:
            events.append("listener_drain")

        async def stop(self) -> None:
            events.append("listener_stop")

    monkeypatch.setattr(robot_module, "robot_worker", Worker())
    monkeypatch.setattr(robot_module, "robot_instance", object())
    monkeypatch.setattr(robot_module, "event_listener", Listener())

    with pytest.raises(RuntimeError, match="shutdown failed"):
        await robot_module.stop_robot_worker()

    assert events == ["worker_shutdown", "listener_drain", "listener_stop"]
    assert robot_module.robot_worker is None
    assert robot_module.robot_instance is None
    assert robot_module.event_listener is None


@pytest.mark.anyio
async def test_stop_robot_worker_clears_globals_when_listener_stop_fails(monkeypatch) -> None:
    class Listener:
        async def drain(self) -> None:
            return None

        async def stop(self) -> None:
            raise RuntimeError("listener stop failed")

    monkeypatch.setattr(robot_module, "robot_worker", None)
    monkeypatch.setattr(robot_module, "robot_instance", object())
    monkeypatch.setattr(robot_module, "event_listener", Listener())

    with pytest.raises(RuntimeError, match="listener stop failed"):
        await robot_module.stop_robot_worker()

    assert robot_module.robot_worker is None
    assert robot_module.robot_instance is None
    assert robot_module.event_listener is None


@pytest.mark.anyio
async def test_stop_robot_worker_is_a_noop_when_no_worker_or_listener(monkeypatch) -> None:
    monkeypatch.setattr(robot_module, "robot_worker", None)
    monkeypatch.setattr(robot_module, "robot_instance", object())
    monkeypatch.setattr(robot_module, "event_listener", None)

    await robot_module.stop_robot_worker()

    assert robot_module.robot_worker is None
    assert robot_module.robot_instance is None
    assert robot_module.event_listener is None


@pytest.mark.anyio
async def test_lifespan_syncs_bins_before_starting_mock_runtime(monkeypatch) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def schema_connection():
        yield SimpleNamespace(run_sync=AsyncMock())

    class Session:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *_args):
            return False

    bin_service = SimpleNamespace(
        sync_config=AsyncMock(side_effect=lambda: events.append("sync_bins")),
        recover_interrupted_workflows=AsyncMock(side_effect=lambda: events.append("recover_workflows")),
    )
    runtime_paths = SimpleNamespace(
        mode=OperatingMode.mock,
        config_path="config.mock.json",
        database_path="base.mock.db",
        calibration_dir="calibration.mock",
    )
    monkeypatch.setattr(main_module.RuntimeStatePaths, "current", lambda: runtime_paths)
    monkeypatch.setattr(main_module, "get_settings", lambda: SimpleNamespace(robot=SimpleNamespace(enabled=False)))
    monkeypatch.setattr(main_module, "_run_migrations", lambda: events.append("migrations"))
    monkeypatch.setattr(main_module, "engine", SimpleNamespace(begin=schema_connection))
    monkeypatch.setattr(main_module, "AsyncSessionLocal", Session)
    monkeypatch.setattr(main_module, "BinStorageService", lambda _session: bin_service)
    monkeypatch.setattr(main_module, "_ensure_default_roles_and_access_levels", AsyncMock())
    monkeypatch.setattr(main_module, "_ensure_default_role_permissions", AsyncMock())
    monkeypatch.setattr(main_module, "_ensure_developer_user", AsyncMock())
    monkeypatch.setattr(main_module, "_apply_mode_transition", AsyncMock())
    monkeypatch.setattr(main_module.onboarding, "ensure_onboarding_state", AsyncMock())
    monkeypatch.setattr(main_module, "start_mock_robot_runtime", AsyncMock(side_effect=lambda: events.append("mock_runtime")))
    monkeypatch.setattr(main_module, "stop_mock_robot_runtime", AsyncMock())
    monkeypatch.setattr(main_module.demo, "shutdown_demo_cycle", AsyncMock())
    monkeypatch.setattr(main_module, "stop_all_camera_streams", AsyncMock())

    async with main_module.lifespan(main_module.app):
        assert events == ["migrations", "sync_bins", "recover_workflows", "mock_runtime"]


@pytest.mark.anyio
async def test_lifespan_recovers_workflows_when_robot_is_disabled(monkeypatch) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def schema_connection():
        yield SimpleNamespace(run_sync=AsyncMock())

    class Session:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *_args):
            return False

    bin_service = SimpleNamespace(
        sync_config=AsyncMock(side_effect=lambda: events.append("sync_bins")),
        recover_interrupted_workflows=AsyncMock(side_effect=lambda: events.append("recover_workflows")),
    )
    runtime_paths = SimpleNamespace(
        mode=OperatingMode.production,
        config_path="config.json",
        database_path="base.db",
        calibration_dir="calibration",
    )
    monkeypatch.setattr(main_module.RuntimeStatePaths, "current", lambda: runtime_paths)
    monkeypatch.setattr(main_module, "get_settings", lambda: SimpleNamespace(robot=SimpleNamespace(enabled=False)))
    monkeypatch.setattr(main_module, "_run_migrations", lambda: None)
    monkeypatch.setattr(main_module, "engine", SimpleNamespace(begin=schema_connection))
    monkeypatch.setattr(main_module, "AsyncSessionLocal", Session)
    monkeypatch.setattr(main_module, "BinStorageService", lambda _session: bin_service)
    monkeypatch.setattr(main_module, "_ensure_default_roles_and_access_levels", AsyncMock())
    monkeypatch.setattr(main_module, "_ensure_default_role_permissions", AsyncMock())
    monkeypatch.setattr(main_module, "_ensure_developer_user", AsyncMock())
    monkeypatch.setattr(main_module, "_apply_mode_transition", AsyncMock())
    monkeypatch.setattr(main_module.onboarding, "ensure_onboarding_state", AsyncMock())
    monkeypatch.setattr(main_module, "_get_tool_camera_url", lambda _settings: None)
    monkeypatch.setattr(main_module, "_get_overhead_camera_url", lambda _settings: None)
    monkeypatch.setattr(main_module, "_get_overview_camera_urls", lambda _settings: {})
    monkeypatch.setattr(main_module, "init_iobox_sizer", AsyncMock())
    monkeypatch.setattr(main_module, "get_iobox_sizer_status", lambda: {"enabled": False})
    monkeypatch.setattr(main_module, "stop_robot_worker", AsyncMock())
    monkeypatch.setattr(main_module, "stop_iobox_sizer", AsyncMock())
    monkeypatch.setattr(main_module.demo, "shutdown_demo_cycle", AsyncMock())
    monkeypatch.setattr(main_module, "start_crash_video_recorder", AsyncMock())
    monkeypatch.setattr(main_module, "stop_crash_video_recorder", AsyncMock())
    monkeypatch.setattr(main_module, "stop_all_camera_streams", AsyncMock())

    async with main_module.lifespan(main_module.app):
        assert events == ["sync_bins", "recover_workflows"]


@pytest.mark.anyio
async def test_lifespan_recovers_workflows_before_hardware_init_failure(monkeypatch) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def schema_connection():
        yield SimpleNamespace(run_sync=AsyncMock())

    class Session:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *_args):
            return False

    async def failed_hardware_initialization() -> None:
        events.append("hardware_init_failure")

    bin_service = SimpleNamespace(
        sync_config=AsyncMock(side_effect=lambda: events.append("sync_bins")),
        recover_interrupted_workflows=AsyncMock(side_effect=lambda: events.append("recover_workflows")),
    )
    runtime_paths = SimpleNamespace(
        mode=OperatingMode.production,
        config_path="config.json",
        database_path="base.db",
        calibration_dir="calibration",
    )
    monkeypatch.setattr(main_module.RuntimeStatePaths, "current", lambda: runtime_paths)
    monkeypatch.setattr(main_module, "get_settings", lambda: SimpleNamespace(robot=SimpleNamespace(enabled=True)))
    monkeypatch.setattr(main_module, "_run_migrations", lambda: None)
    monkeypatch.setattr(main_module, "engine", SimpleNamespace(begin=schema_connection))
    monkeypatch.setattr(main_module, "AsyncSessionLocal", Session)
    monkeypatch.setattr(main_module, "BinStorageService", lambda _session: bin_service)
    monkeypatch.setattr(main_module, "_ensure_default_roles_and_access_levels", AsyncMock())
    monkeypatch.setattr(main_module, "_ensure_default_role_permissions", AsyncMock())
    monkeypatch.setattr(main_module, "_ensure_developer_user", AsyncMock())
    monkeypatch.setattr(main_module, "_apply_mode_transition", AsyncMock())
    monkeypatch.setattr(main_module.onboarding, "ensure_onboarding_state", AsyncMock())
    monkeypatch.setattr(main_module, "init_robot_worker", failed_hardware_initialization)
    monkeypatch.setattr(main_module, "_get_tool_camera_url", lambda _settings: None)
    monkeypatch.setattr(main_module, "_get_overhead_camera_url", lambda _settings: None)
    monkeypatch.setattr(main_module, "_get_overview_camera_urls", lambda _settings: {})
    monkeypatch.setattr(main_module, "init_iobox_sizer", AsyncMock())
    monkeypatch.setattr(main_module, "get_iobox_sizer_status", lambda: {"enabled": False})
    monkeypatch.setattr(main_module, "stop_robot_worker", AsyncMock())
    monkeypatch.setattr(main_module, "stop_iobox_sizer", AsyncMock())
    monkeypatch.setattr(main_module.demo, "shutdown_demo_cycle", AsyncMock())
    monkeypatch.setattr(main_module, "start_crash_video_recorder", AsyncMock())
    monkeypatch.setattr(main_module, "stop_crash_video_recorder", AsyncMock())
    monkeypatch.setattr(main_module, "stop_all_camera_streams", AsyncMock())

    async with main_module.lifespan(main_module.app):
        await asyncio.sleep(0)
        assert events == ["sync_bins", "recover_workflows", "hardware_init_failure"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("has_active_bin_workflow", "expected_home_on_init"),
    [(False, True), (True, False)],
)
async def test_robot_initialization_checks_recovered_bin_workflows_before_configuring_motion(
    monkeypatch, has_active_bin_workflow, expected_home_on_init,
):
    events: list[str] = []
    robot = MagicMock()
    robot.get_init_component_names.return_value = []
    robot.configure = AsyncMock(side_effect=lambda **_kwargs: events.append("configure"))
    worker = MagicMock()
    worker.connect = AsyncMock()
    worker.start = AsyncMock()
    worker.get_status_snapshot = AsyncMock()
    listener = MagicMock()
    listener.start = AsyncMock()
    storage_service = MagicMock()
    storage_service.get_all_occupied_cells = AsyncMock(return_value=[])

    async def read_active_bin_workflow() -> bool:
        events.append("active_workflow")
        return has_active_bin_workflow

    monkeypatch.setattr(robot_module, "validate_robot_config", lambda: None)
    monkeypatch.setattr(robot_module.r, "Robot", lambda: robot)
    monkeypatch.setattr(
        robot_module,
        "_has_active_bin_workflow",
        read_active_bin_workflow,
    )
    monkeypatch.setattr(robot_module, "StorageService", lambda _session: storage_service)
    monkeypatch.setattr(robot_module, "reserved_bin_locations", AsyncMock(return_value=set()))
    monkeypatch.setattr(robot_module.r_worker, "RobotWorker", lambda _robot, **_kwargs: worker)
    monkeypatch.setattr(robot_module, "RobotEventListener", lambda _worker: listener)
    monkeypatch.setattr(robot_module, "_barcode_enabled", lambda: False)
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: False)
    monkeypatch.setattr(robot_module, "_home_on_init_enabled", lambda: True)
    monkeypatch.setattr(robot_module, "robot_worker", None)
    monkeypatch.setattr(robot_module, "robot_instance", None)
    monkeypatch.setattr(robot_module, "event_listener", None)
    get_init_tracker().reset()

    class Session:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(robot_module, "AsyncSessionLocal", Session)

    await robot_module.init_robot_worker()

    assert events == ["active_workflow", "configure"]
    robot.configure.assert_awaited_once_with(isolate=True, home_on_init=expected_home_on_init)
    if has_active_bin_workflow:
        worker.set_needs_home.assert_called_once_with(
            "Automatic homing disabled on initialization",
            axes=sorted(robot_module.CRITICAL_MOTOR_COMPONENTS),
        )
    else:
        worker.set_needs_home.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("has_active_bin_workflow", "failed_motor_components", "expected_reason", "expected_axes"),
    [
        (
            True,
            [],
            "Automatic homing disabled on initialization",
            sorted(robot_module.CRITICAL_MOTOR_COMPONENTS),
        ),
        (
            False,
            ["x_axis"],
            "Startup failed for motion components: x_axis",
            ["x_axis"],
        ),
    ],
)
async def test_robot_initialization_blocks_startup_scans_before_worker_exposure(
    monkeypatch,
    has_active_bin_workflow,
    failed_motor_components,
    expected_reason,
    expected_axes,
):
    events: list[str] = []
    robot = MagicMock()
    robot.get_init_component_names.return_value = ["x_axis"]

    async def configure(**_kwargs) -> None:
        events.append("configure")
        for component in failed_motor_components:
            get_init_tracker().report(component, "failed", reason="startup fault")

    robot.configure = configure

    class StartupWorker:
        def __init__(self) -> None:
            self.is_needs_home = False
            self.return_enqueued = False

        def set_needs_home(self, reason, *, axes) -> None:
            assert reason == expected_reason
            assert axes == expected_axes
            self.is_needs_home = True
            events.append("needs_home")

        def inject_awaiting_operator(self, boundary: str) -> None:
            if self.is_needs_home:
                events.append(f"{boundary}_scan_blocked")
            else:
                self.return_enqueued = True
                events.append(f"{boundary}_return_enqueued")

        async def connect(self) -> None:
            self.inject_awaiting_operator("scanner_connect")

        async def start(self) -> None:
            self.inject_awaiting_operator("command_start")

        async def get_status_snapshot(self, force_refresh=False) -> None:
            return None

        async def shutdown(self) -> None:
            return None

    worker = StartupWorker()

    class Listener:
        async def start(self) -> None:
            assert robot_module.robot_worker is None
            worker.inject_awaiting_operator("event_listener")

        async def stop(self) -> None:
            return None

    listener = Listener()
    storage_service = MagicMock()
    storage_service.get_all_occupied_cells = AsyncMock(return_value=[])

    async def read_active_bin_workflow() -> bool:
        events.append("active_workflow")
        return has_active_bin_workflow

    async def report_scanner_status(candidate) -> None:
        assert candidate is worker
        worker.inject_awaiting_operator("scanner_status")

    class Session:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(robot_module, "validate_robot_config", lambda: None)
    monkeypatch.setattr(robot_module.r, "Robot", lambda: robot)
    monkeypatch.setattr(
        robot_module,
        "_has_active_bin_workflow",
        read_active_bin_workflow,
    )
    monkeypatch.setattr(robot_module, "StorageService", lambda _session: storage_service)
    monkeypatch.setattr(robot_module, "reserved_bin_locations", AsyncMock(return_value=set()))
    monkeypatch.setattr(robot_module.r_worker, "RobotWorker", lambda _robot, **_kwargs: worker)
    monkeypatch.setattr(robot_module, "RobotEventListener", lambda _worker: listener)
    monkeypatch.setattr(robot_module, "_report_scanner_status", report_scanner_status)
    monkeypatch.setattr(robot_module, "_barcode_enabled", lambda: True)
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: False)
    monkeypatch.setattr(robot_module, "_home_on_init_enabled", lambda: True)
    monkeypatch.setattr(robot_module, "robot_worker", None)
    monkeypatch.setattr(robot_module, "robot_instance", None)
    monkeypatch.setattr(robot_module, "event_listener", None)
    monkeypatch.setattr(robot_module, "AsyncSessionLocal", Session)
    get_init_tracker().reset()

    await robot_module.init_robot_worker()

    assert events == [
        "active_workflow",
        "configure",
        "needs_home",
        "scanner_connect_scan_blocked",
        "scanner_status_scan_blocked",
        "command_start_scan_blocked",
        "event_listener_scan_blocked",
    ]
    assert worker.return_enqueued is False
    assert robot_module.robot_worker is worker
