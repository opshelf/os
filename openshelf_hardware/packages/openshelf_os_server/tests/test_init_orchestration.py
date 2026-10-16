import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import openshelf_os_server.core.robot as robot_module
import openshelf_os_server.main as main_module
from openshelf_os_server.core.init_progress import get_init_tracker
from openshelf_os.operating_mode import OperatingMode


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
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            robot=SimpleNamespace(enabled=True),
            auth=SimpleNamespace(dev_mode=False, dev_auto_user=None),
        ),
    )
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
