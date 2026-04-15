import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from perpetual_os1 import laser_angle_calibration as angle_module
from perpetual_os1.laser_scan import ScanBounds
from perpetual_os1.storage import StorageModuleSide
from perpetual_os1_server.core.laser_auto_calibration import (
    _build_angle_rotation_center_deg,
    _build_angle_x_positions,
    _build_angle_z_offset_positions,
    _build_derived_right_angle_result,
    _SideState,
    LaserAutoCalibrationAngleResult,
    LaserAutoCalibrationManager,
    _build_estimated_rotation_deg,
    _build_valid_rotation_positions,
)
from perpetual_os1_server.routers import robot as robot_router
from perpetual_os1_server.core.laser_auto_calibration import LaserAutoCalibrationSnapshot
from perpetual_os1_server.routers import calibration as calibration_router


class FakeLaserManager:
    def __init__(self, *, snapshot: LaserAutoCalibrationSnapshot, is_active: bool) -> None:
        self.snapshot = snapshot
        self.is_active_value = is_active
        self.ensure_calls = 0
        self.unsubscribe_calls = 0
        self.queue: asyncio.Queue[LaserAutoCalibrationSnapshot] = asyncio.Queue()

    async def is_active(self) -> bool:
        return self.is_active_value

    async def ensure_session_started(self, worker) -> LaserAutoCalibrationSnapshot:
        self.ensure_calls += 1
        self.is_active_value = True
        return self.snapshot

    async def subscribe(self) -> tuple[asyncio.Queue[LaserAutoCalibrationSnapshot], LaserAutoCalibrationSnapshot]:
        return self.queue, self.snapshot

    async def unsubscribe(self, queue: asyncio.Queue[LaserAutoCalibrationSnapshot]) -> None:
        self.unsubscribe_calls += 1

    async def get_snapshot(self) -> LaserAutoCalibrationSnapshot:
        return self.snapshot

    async def abort(self) -> LaserAutoCalibrationSnapshot:
        return self.snapshot


def _build_snapshot(state: str = "running") -> LaserAutoCalibrationSnapshot:
    return LaserAutoCalibrationSnapshot(
        state=state,
        session_id="laser-session",
        run_id="left-run",
        current_side="left_module",
        current_stage="shelf-scout",
        bounds=ScanBounds(x_min=0.0, x_max=945.0, y_min=0.0, y_max=1200.0),
        started_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:01+00:00",
        scouted_shelf_count=2,
        candidate_count=3,
        detected_cell_count=0,
        stage_detail={"side": "left_module", "phase": "shelf-scout"},
    )


def test_build_valid_rotation_positions_clips_out_of_range_targets() -> None:
    scan_config = SimpleNamespace(
        angle_calibration_rotation_min_deg=-8.0,
        angle_calibration_rotation_max_deg=8.0,
        angle_calibration_rotation_step_deg=1.0,
    )

    rotation_positions = _build_valid_rotation_positions(scan_config, 4.2)

    assert rotation_positions[0] == -4.0
    assert rotation_positions[-1] == 8.0
    assert -5.0 not in rotation_positions


def test_build_angle_x_positions_runs_from_high_x_to_low_x() -> None:
    scan_config = SimpleNamespace(
        angle_calibration_x_min_mm=50.0,
        angle_calibration_x_max_mm=940.0,
        angle_calibration_x_step_mm=100.0,
        angle_calibration_x_count=5,
    )

    x_positions = _build_angle_x_positions(scan_config)

    assert x_positions[0] == 940.0
    assert x_positions[-1] == 540.0
    assert len(x_positions) == 5


def test_build_angle_z_offset_positions_prefers_center_slice() -> None:
    scan_config = SimpleNamespace(
        angle_calibration_z_offset_min_mm=-8.0,
        angle_calibration_z_offset_max_mm=8.0,
        angle_calibration_z_offset_step_mm=4.0,
        angle_calibration_z_offset_count=1,
    )

    z_offset_positions = _build_angle_z_offset_positions(scan_config)

    assert z_offset_positions == [0.0]


def test_build_angle_rotation_center_deg_uses_front_for_left() -> None:
    assert _build_angle_rotation_center_deg(StorageModuleSide.LEFT) == 0.0


def test_build_angle_rotation_center_deg_uses_back_for_right() -> None:
    assert _build_angle_rotation_center_deg(StorageModuleSide.RIGHT) == 180.0


def test_build_estimated_rotation_deg_offsets_previous_rotation() -> None:
    fit_result = SimpleNamespace(alpha_deg=4.2)

    estimated_rotation_deg = _build_estimated_rotation_deg(172.8, fit_result)

    assert estimated_rotation_deg == pytest.approx(177.0)


def test_build_derived_right_angle_result_uses_left_rotation_complement() -> None:
    left_angle_result = LaserAutoCalibrationAngleResult(
        side='left_module',
        alpha_deg=3.815,
        sample_count=45,
        capture_y_mm=1095.0,
        rms_residual_mm=0.1,
        max_abs_residual_mm=0.2,
        line_angle_deg=90.0,
        previous_rotation_deg=0.0,
        updated_rotation_deg=3.815,
    )

    right_angle_result = _build_derived_right_angle_result(180.0, left_angle_result)

    assert right_angle_result.updated_rotation_deg == pytest.approx(183.815)
    assert right_angle_result.previous_rotation_deg == pytest.approx(180.0)


@pytest.mark.asyncio
async def test_home_after_abort_homes_tool_and_door() -> None:
    manager = LaserAutoCalibrationManager()
    tool = SimpleNamespace(home=AsyncMock())
    io = SimpleNamespace(home_door=AsyncMock())
    worker = SimpleNamespace(
        _robot=SimpleNamespace(_tool=tool, _io=io),
        clear_needs_home=MagicMock(),
        get_status_snapshot=AsyncMock(),
    )

    await manager._home_after_abort(worker)

    tool.home.assert_awaited_once()
    io.home_door.assert_awaited_once()
    worker.clear_needs_home.assert_called_once()
    worker.get_status_snapshot.assert_awaited_once_with(force_refresh=True)


@pytest.mark.asyncio
async def test_abort_stops_tool_while_session_cancels() -> None:
    manager = LaserAutoCalibrationManager()
    robot = SimpleNamespace(stop=AsyncMock())
    worker = SimpleNamespace(_robot=robot)

    async def did_run() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(did_run())

    async with manager._lock:
        manager._active_worker = worker
        manager._task = task
        manager._snapshot = LaserAutoCalibrationSnapshot(
            state='running',
            session_id='laser-session',
            current_stage='angle-calibration',
            updated_at='2026-01-01T00:00:00+00:00',
        )

    snapshot = await manager.abort()

    robot.stop.assert_awaited_once()
    assert snapshot.state == 'running'
    assert snapshot.current_stage == 'cancelling'


@pytest.mark.asyncio
async def test_run_session_cancel_marks_needs_home_without_abort_homing() -> None:
    manager = LaserAutoCalibrationManager()
    worker = SimpleNamespace(
        set_needs_home=MagicMock(),
        get_status_snapshot=AsyncMock(),
    )

    with patch.object(manager, '_get_current_rotations', AsyncMock(return_value={})):
        with patch.object(manager, '_run_all_sides', AsyncMock(side_effect=asyncio.CancelledError)):
            with patch.object(manager, '_restore_in_memory_rotations') as restore_mock:
                with patch.object(manager, '_set_terminal_state', AsyncMock()) as terminal_mock:
                    with patch.object(manager, '_home_after_abort', AsyncMock()) as home_mock:
                        with pytest.raises(asyncio.CancelledError):
                            await manager._run_session(worker, 'laser-session')

    restore_mock.assert_called_once_with(worker, {})
    worker.set_needs_home.assert_called_once_with('Laser auto calibration aborted')
    worker.get_status_snapshot.assert_awaited_once_with(force_refresh=True)
    terminal_mock.assert_awaited_once_with('cancelled', None)
    home_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_all_sides_processes_right_before_left() -> None:
    manager = LaserAutoCalibrationManager()
    tool = SimpleNamespace(get_param=AsyncMock(return_value=180.0))
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    processed_sides: list[StorageModuleSide] = []
    left_angle_result = LaserAutoCalibrationAngleResult(
        side='left_module',
        alpha_deg=3.815,
        sample_count=45,
        capture_y_mm=1095.0,
        rms_residual_mm=0.1,
        max_abs_residual_mm=0.2,
        line_angle_deg=90.0,
        previous_rotation_deg=0.0,
        updated_rotation_deg=3.815,
    )

    async def did_run_side(current_worker, side: StorageModuleSide, angle_result: LaserAutoCalibrationAngleResult):
        assert current_worker is worker
        processed_sides.append(side)

        return SimpleNamespace(side=side, angle_result=angle_result)

    with patch('perpetual_os1_server.core.laser_auto_calibration._build_bounds_for_side', return_value=ScanBounds(x_min=0.0, x_max=1.0, y_min=0.0, y_max=1.0)):
        with patch.object(manager, '_run_angle_calibration_for_side', AsyncMock(return_value=left_angle_result)):
            with patch.object(manager, '_apply_angle_calibration_in_memory', AsyncMock()) as apply_mock:
                with patch.object(manager, '_run_side', side_effect=did_run_side):
                    with patch.object(manager, '_persist_completed_results', AsyncMock()) as persist_mock:
                        await manager._run_all_sides(worker)

    assert processed_sides == [StorageModuleSide.RIGHT, StorageModuleSide.LEFT]
    assert apply_mock.await_count == 2
    persist_mock.assert_awaited_once()

    persisted_results = persist_mock.await_args.args[1]

    assert [result.side for result in persisted_results] == [StorageModuleSide.RIGHT, StorageModuleSide.LEFT]
    assert persisted_results[0].angle_result.updated_rotation_deg == pytest.approx(183.815)


@pytest.mark.asyncio
async def test_sync_snapshot_uses_live_counts_when_metadata_lags() -> None:
    manager = LaserAutoCalibrationManager()
    manager._side_states['left_module'] = _SideState(
        live_shelf_count=7,
        live_candidate_count=11,
        live_detected_cell_count=3,
    )
    manager._side_states['right_module'] = _SideState(
        live_shelf_count=2,
        live_candidate_count=5,
        live_detected_cell_count=1,
    )

    snapshot = await manager._sync_snapshot()

    assert snapshot.left_scouted_shelf_count == 7
    assert snapshot.right_scouted_shelf_count == 2
    assert snapshot.left_candidate_count == 11
    assert snapshot.right_candidate_count == 5
    assert snapshot.left_detected_cell_count == 3
    assert snapshot.right_detected_cell_count == 1
    assert snapshot.scouted_shelf_count == 9
    assert snapshot.candidate_count == 16
    assert snapshot.detected_cell_count == 4


@pytest.mark.asyncio
async def test_resolve_capture_y_mm_stops_after_first_detected_edge() -> None:
    tool = SimpleNamespace(retract=AsyncMock())
    args = SimpleNamespace(y_mm=None, y_range=(0.0, 100.0), scout_x_mm=None)
    scan_config = SimpleNamespace()
    first_edges = [angle_module.ShelfSurfaceEdge(x_mm=10.0, y_mm=72.0)]

    with patch.object(angle_module, 'build_shelf_scout_x_positions', return_value=[10.0, 20.0]):
        with patch.object(angle_module, 'build_axis_positions', return_value=[100.0, 98.0, 96.0]):
            with patch.object(angle_module, 'set_y_brake_for_shelf_scouting', AsyncMock()):
                with patch.object(angle_module, 'enter_capture_mode', AsyncMock()):
                    with patch.object(angle_module, 'prepare_capture_pose', AsyncMock()):
                        with patch.object(angle_module, 'filter_shelf_edges', return_value=first_edges):
                            with patch.object(
                                angle_module,
                                'scan_shelf_scout_line',
                                AsyncMock(return_value=([], first_edges)),
                            ) as scan_mock:
                                target_y_mm = await angle_module.resolve_capture_y_mm(
                                    tool,
                                    args=args,
                                    scan_config=scan_config,
                                    side_name='left',
                                    side_number=0,
                                    x_positions=[10.0, 20.0],
                                    rotation_center_deg=0.0,
                                    stage_extension_mm=13.0,
                                    base_z_mm=10.0,
                                    z_move_rpm=100.0,
                                    z_move_accel=100.0,
                                    settle_ms=0,
                                    samples_per_point=1,
                                )

    assert target_y_mm == 72.0
    assert scan_mock.await_count == 1
    assert scan_mock.await_args.kwargs['stop_after_first_edge'] is True


@pytest.mark.asyncio
async def test_resolve_capture_y_mm_enters_right_shelf_scout_at_180_deg() -> None:
    tool = SimpleNamespace(retract=AsyncMock())
    args = SimpleNamespace(y_mm=None, y_range=(0.0, 100.0), scout_x_mm=None)
    scan_config = SimpleNamespace()
    first_edges = [angle_module.ShelfSurfaceEdge(x_mm=25.0, y_mm=72.0)]

    with patch.object(angle_module, 'build_shelf_scout_x_positions', return_value=[25.0]):
        with patch.object(angle_module, 'build_axis_positions', return_value=[100.0, 98.0, 96.0]):
            with patch.object(angle_module, 'set_y_brake_for_shelf_scouting', AsyncMock()):
                with patch.object(angle_module, 'enter_capture_mode', AsyncMock()) as enter_mock:
                    with patch.object(angle_module, 'filter_shelf_edges', return_value=first_edges):
                        with patch.object(
                            angle_module,
                            'scan_shelf_scout_line',
                            AsyncMock(return_value=([], first_edges)),
                        ):
                            await angle_module.resolve_capture_y_mm(
                                tool,
                                args=args,
                                scan_config=scan_config,
                                side_name='right',
                                side_number=1,
                                x_positions=[945.0, 845.0],
                                rotation_center_deg=180.0,
                                stage_extension_mm=13.0,
                                base_z_mm=140.5,
                                z_move_rpm=100.0,
                                z_move_accel=100.0,
                                settle_ms=0,
                                samples_per_point=1,
                            )

    assert enter_mock.await_args.kwargs['absolute_rotation_deg'] == 180.0


@pytest.mark.asyncio
async def test_get_laser_auto_calibration_status(client) -> None:
    manager = FakeLaserManager(snapshot=_build_snapshot("idle"), is_active=False)

    with patch(
        "perpetual_os1_server.routers.calibration.get_laser_auto_calibration_manager",
        return_value=manager,
    ):
        response = await client.get("/api/calibration/laser/status")

    assert response.status_code == 200
    assert response.json()["state"] == "idle"


@pytest.mark.asyncio
async def test_stream_laser_auto_calibration_starts_session_when_idle() -> None:
    snapshot = _build_snapshot()
    manager = FakeLaserManager(snapshot=snapshot, is_active=False)
    worker = SimpleNamespace(
        _current_command=None,
        _demo_cycling=False,
        needs_home=lambda: False,
        needs_home_reason=lambda: None,
        needs_home_axes=lambda: [],
    )

    with patch(
        "perpetual_os1_server.routers.calibration.get_laser_auto_calibration_manager",
        return_value=manager,
    ):
        with patch(
            "perpetual_os1_server.routers.calibration.get_robot_worker",
            AsyncMock(return_value=worker),
        ):
            request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
            response = await calibration_router.stream_laser_auto_calibration(request)
            chunk = await anext(response.body_iterator)
            await response.body_iterator.aclose()

    payload = json.loads(chunk["data"])
    assert payload["state"] == "running"
    assert payload["current_stage"] == "shelf-scout"

    assert manager.ensure_calls == 1


@pytest.mark.asyncio
async def test_stream_laser_auto_calibration_reuses_running_session() -> None:
    snapshot = _build_snapshot()
    manager = FakeLaserManager(snapshot=snapshot, is_active=True)

    with patch(
        "perpetual_os1_server.routers.calibration.get_laser_auto_calibration_manager",
        return_value=manager,
    ):
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        response = await calibration_router.stream_laser_auto_calibration(request)
        chunk = await anext(response.body_iterator)
        await response.body_iterator.aclose()

    payload = json.loads(chunk["data"])
    assert payload["session_id"] == "laser-session"

    assert manager.ensure_calls == 0


@pytest.mark.asyncio
async def test_abort_laser_auto_calibration(client) -> None:
    manager = FakeLaserManager(snapshot=_build_snapshot("cancelled"), is_active=True)

    with patch(
        "perpetual_os1_server.routers.calibration.get_laser_auto_calibration_manager",
        return_value=manager,
    ):
        response = await client.post("/api/calibration/laser/abort")

    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"


@pytest.mark.asyncio
async def test_move_is_blocked_while_laser_auto_calibration_is_running(client) -> None:
    manager = FakeLaserManager(snapshot=_build_snapshot(), is_active=True)

    with patch(
        "perpetual_os1_server.routers.calibration.get_laser_auto_calibration_manager",
        return_value=manager,
    ):
        response = await client.post(
            "/api/calibration/move",
            json={"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "laser auto calibration is running"


@pytest.mark.asyncio
async def test_demo_cycle_start_is_blocked_while_laser_auto_calibration_is_running(client) -> None:
    with patch(
        "perpetual_os1_server.routers.demo.is_laser_auto_calibration_running",
        AsyncMock(return_value=True),
    ):
        response = await client.post("/api/demo/cycle/start")

    assert response.status_code == 409
    assert response.json()["detail"] == "laser auto calibration is running"


@pytest.mark.asyncio
async def test_robot_commands_are_blocked_while_laser_auto_calibration_is_running() -> None:
    with patch(
        "perpetual_os1_server.routers.robot.is_laser_auto_calibration_running",
        AsyncMock(return_value=True),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await robot_router.robot_commands(
                command=object(),
                client=SimpleNamespace(),
                worker=SimpleNamespace(),
                current_user=SimpleNamespace(),
                db=SimpleNamespace(),
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "laser auto calibration is running"
