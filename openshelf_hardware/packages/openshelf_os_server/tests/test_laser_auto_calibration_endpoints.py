import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from openshelf_os.laser_calibration import angle as angle_module
from openshelf_os import laser_calibration as laser_module
from openshelf_os.common import Parameters
from openshelf_os.laser_calibration.scan import ScanBounds, ScanDetectedCell
from openshelf_os.storage import StorageModuleSide
from openshelf_os_server.core.laser_auto_calibration import (
    _build_angle_rotation_center_deg,
    _build_default_scans_dir,
    _build_angle_x_positions,
    _build_bounds_for_side,
    _build_stage_extension_probe_y_mm,
    _build_stage_stall_fine_rotation_positions,
    _build_stage_stall_rotation_positions,
    _build_stage_stall_rotation_center_deg,
    _resolve_io_box_laser_offset_x_mm,
    _SideState,
    LaserAutoCalibrationAngleResult,
    LaserAutoCalibrationScope,
    LaserAutoCalibrationManager,
)
from openshelf_os_server.core.laser_auto_calibration.angle_runner import (
    _build_stage_stall_validation_rotation_positions,
    _select_stage_stall_best_summary,
)
from openshelf_os_server.core.laser_auto_calibration.scope_runner import (
    _validate_laser_auto_calibration_config,
)
from openshelf_os_server.routers import robot as robot_router
from openshelf_os_server.core.laser_auto_calibration import LaserAutoCalibrationSnapshot
from openshelf_os_server.routers import calibration as calibration_router


class FakeLaserManager:
    def __init__(self, *, snapshot: LaserAutoCalibrationSnapshot, is_active: bool) -> None:
        self.snapshot = snapshot
        self.is_active_value = is_active
        self.ensure_calls = 0
        self.last_requested_scope: LaserAutoCalibrationScope | None = None
        self.unsubscribe_calls = 0
        self.queue: asyncio.Queue[LaserAutoCalibrationSnapshot] = asyncio.Queue()

    async def is_active(self) -> bool:
        return self.is_active_value

    async def ensure_session_started(self, worker, requested_scope=None) -> LaserAutoCalibrationSnapshot:
        self.ensure_calls += 1
        self.is_active_value = True

        if requested_scope is not None:
            self.last_requested_scope = requested_scope
            self.snapshot.requested_scope = requested_scope

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
        requested_scope=LaserAutoCalibrationScope(include_io_box=True),
        current_side="left_module",
        current_stage="shelf-scout",
        bounds=ScanBounds(x_min=0.0, x_max=950.0, y_min=0.0, y_max=1250.0),
        started_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:01+00:00",
        scouted_shelf_count=2,
        candidate_count=3,
        detected_cell_count=0,
        stage_detail={"side": "left_module", "phase": "shelf-scout"},
    )



def test_build_default_scans_dir_uses_config_data_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "data" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")

    with patch("openshelf_os_server.core.laser_auto_calibration.get_config_path", return_value=config_path):
        assert _build_default_scans_dir() == str((config_path.parent / "calibration").resolve())




def test_build_stage_stall_rotation_positions_uses_wide_coarse_defaults() -> None:
    scan_config = SimpleNamespace()

    rotation_positions = _build_stage_stall_rotation_positions(
        scan_config,
        StorageModuleSide.RIGHT,
        0.0,
        360.0,
    )

    assert rotation_positions[0] == pytest.approx(5.0)
    assert rotation_positions[-1] == pytest.approx(0.0)
    assert 0.0 in rotation_positions
    assert 5.5 in rotation_positions
    assert 6.0 not in rotation_positions
    assert rotation_positions[1] == pytest.approx(4.0)


def test_build_stage_stall_rotation_center_uses_configured_side_angles() -> None:
    parameters = SimpleNamespace(ROTATION_FRONT=2.7, ROTATION_BACK=183.0)

    assert _build_stage_stall_rotation_center_deg(parameters, StorageModuleSide.RIGHT) == pytest.approx(183.0)
    assert _build_stage_stall_rotation_center_deg(parameters, StorageModuleSide.LEFT) == pytest.approx(2.7)


def test_build_stage_stall_fine_rotation_positions_centers_on_coarse_best() -> None:
    scan_config = SimpleNamespace(
        angle_calibration_stage_stall_fine_window_deg=1.5,
        angle_calibration_stage_stall_fine_step_deg=0.25,
    )

    rotation_positions = _build_stage_stall_fine_rotation_positions(
        scan_config,
        4.0,
        0.0,
        360.0,
    )

    assert rotation_positions[0] == pytest.approx(2.5)
    assert rotation_positions[-1] == pytest.approx(5.5)
    assert 4.0 in rotation_positions


def test_build_stage_stall_validation_positions_includes_high_side_window() -> None:
    fine_summaries = [
        {"rotation_deg": 3.75, "median_stage_position_mm": 42.53},
        {"rotation_deg": 4.0, "median_stage_position_mm": 42.23},
        {"rotation_deg": 4.25, "median_stage_position_mm": 29.21},
        {"rotation_deg": 4.5, "median_stage_position_mm": 29.19},
    ]

    rotation_positions = _build_stage_stall_validation_rotation_positions(
        fine_summaries=fine_summaries,
        coarse_summaries=[],
        validation_count=2,
        validation_repeats=3,
        coarse_validation_count=0,
        high_side_window_deg=0.75,
    )

    assert rotation_positions == [
        3.75,
        3.75,
        3.75,
        4.0,
        4.0,
        4.0,
        4.25,
        4.25,
        4.25,
        4.5,
        4.5,
        4.5,
    ]


def test_select_stage_stall_best_summary_chooses_larger_plateau_angle() -> None:
    summaries = [
        {"rotation_deg": 3.75, "absolute_rotation_deg": 3.75, "median_stage_position_mm": 42.265, "valid_stall_count": 1},
        {"rotation_deg": 4.0, "absolute_rotation_deg": 4.0, "median_stage_position_mm": 42.14, "valid_stall_count": 1},
        {"rotation_deg": 4.5, "absolute_rotation_deg": 4.5, "median_stage_position_mm": 41.725, "valid_stall_count": 1},
    ]

    best_summary = _select_stage_stall_best_summary(
        summaries,
        min_stage_position_mm=40.0,
        plateau_tolerance_mm=0.75,
    )

    assert best_summary is not None
    assert best_summary["rotation_deg"] == pytest.approx(4.5)


def test_select_stage_stall_best_summary_requires_valid_sample_count() -> None:
    summaries = [
        {
            "rotation_deg": 4.0,
            "absolute_rotation_deg": 4.0,
            "median_stage_position_mm": 42.0,
            "valid_stall_count": 1,
            "stall_count": 3,
        },
        {
            "rotation_deg": 4.25,
            "absolute_rotation_deg": 4.25,
            "median_stage_position_mm": 41.8,
            "valid_stall_count": 2,
            "stall_count": 3,
        },
    ]

    best_summary = _select_stage_stall_best_summary(
        summaries,
        min_stage_position_mm=40.0,
        plateau_tolerance_mm=0.75,
        min_valid_sample_count=2,
    )

    assert best_summary is not None
    assert best_summary["rotation_deg"] == pytest.approx(4.25)

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




def test_build_angle_rotation_center_deg_uses_configured_front_for_left() -> None:
    parameters = SimpleNamespace(ROTATION_FRONT=184.5, ROTATION_BACK=4.5)

    assert _build_angle_rotation_center_deg(parameters, StorageModuleSide.LEFT) == 184.5


def test_build_angle_rotation_center_deg_uses_configured_back_for_right() -> None:
    parameters = SimpleNamespace(ROTATION_FRONT=184.5, ROTATION_BACK=4.5)

    assert _build_angle_rotation_center_deg(parameters, StorageModuleSide.RIGHT) == 4.5


def test_build_bounds_for_side_clamps_x_min_away_from_zero() -> None:
    scan_config = SimpleNamespace(
        left_bounds_x_min_mm=0.0,
        left_bounds_x_max_mm=950.0,
        left_bounds_y_min_mm=0.0,
        left_bounds_y_max_mm=1250.0,
        right_bounds_x_min_mm=0.0,
        right_bounds_x_max_mm=950.0,
        right_bounds_y_min_mm=0.0,
        right_bounds_y_max_mm=1250.0,
    )

    left_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.LEFT)
    right_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.RIGHT)

    assert left_bounds.x_min == pytest.approx(5.0)
    assert right_bounds.x_min == pytest.approx(5.0)


def _build_reference_bounds_scan_config(**overrides) -> SimpleNamespace:
    values = {
        "laser_offset_x_mm": 50.0,
        "laser_offset_y_mm": 0.0,
        "left_bounds_x_min_mm": 0.0,
        "left_bounds_x_max_mm": 950.0,
        "left_bounds_y_min_mm": 0.0,
        "left_bounds_y_max_mm": 1250.0,
        "right_bounds_x_min_mm": 0.0,
        "right_bounds_x_max_mm": 950.0,
        "right_bounds_y_min_mm": 0.0,
        "right_bounds_y_max_mm": 1250.0,
        "left_reference": SimpleNamespace(
            shelf_idx=0,
            cell_idx=0,
            anchor="top_left",
            x_mm=100.0,
            y_mm=100.0,
            z_offset_mm=15.0,
            wall_depth_mm=9.0,
        ),
        "right_reference": SimpleNamespace(
            shelf_idx=0,
            cell_idx=0,
            anchor="top_left",
            x_mm=100.0,
            y_mm=100.0,
            z_offset_mm=15.0,
            wall_depth_mm=9.0,
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_validate_laser_auto_calibration_config_rejects_all_scope_reference_outside_bounds() -> None:
    scan_config = _build_reference_bounds_scan_config(
        left_reference=SimpleNamespace(
            shelf_idx=0,
            cell_idx=0,
            anchor="top_left",
            x_mm=40.0,
            y_mm=100.0,
            z_offset_mm=15.0,
            wall_depth_mm=9.0,
        ),
    )

    with pytest.raises(ValueError, match="xy_reference x_mm resolves outside scan X bounds"):
        _validate_laser_auto_calibration_config(
            scan_config=scan_config,
            requested_scope=LaserAutoCalibrationScope(mode="all"),
        )



def test_validate_laser_auto_calibration_config_rejects_both_scope_reference_outside_bounds() -> None:
    scan_config = _build_reference_bounds_scan_config(
        left_reference=SimpleNamespace(
            shelf_idx=0,
            cell_idx=0,
            anchor="top_left",
            x_mm=40.0,
            y_mm=100.0,
            z_offset_mm=15.0,
            wall_depth_mm=9.0,
        ),
    )

    with pytest.raises(ValueError, match="xy_reference x_mm resolves outside scan X bounds"):
        _validate_laser_auto_calibration_config(
            scan_config=scan_config,
            requested_scope=LaserAutoCalibrationScope(mode="both"),
        )

def test_validate_laser_auto_calibration_config_rejects_side_scope_reference_outside_bounds() -> None:
    scan_config = _build_reference_bounds_scan_config(
        right_reference=SimpleNamespace(
            shelf_idx=0,
            cell_idx=0,
            anchor="top_left",
            x_mm=900.0,
            y_mm=100.0,
            z_offset_mm=15.0,
            wall_depth_mm=9.0,
        ),
    )

    with pytest.raises(ValueError, match="xy_reference x_mm resolves outside scan X bounds"):
        _validate_laser_auto_calibration_config(
            scan_config=scan_config,
            requested_scope=LaserAutoCalibrationScope(mode="side", side=StorageModuleSide.RIGHT),
        )


def test_validate_laser_auto_calibration_config_allows_local_shelf_scope_reference_outside_full_bounds() -> None:
    scan_config = _build_reference_bounds_scan_config(
        right_reference=SimpleNamespace(
            shelf_idx=0,
            cell_idx=0,
            anchor="top_left",
            x_mm=900.0,
            y_mm=100.0,
            z_offset_mm=15.0,
            wall_depth_mm=9.0,
        ),
    )

    _validate_laser_auto_calibration_config(
        scan_config=scan_config,
        requested_scope=LaserAutoCalibrationScope(
            mode="shelf",
            side=StorageModuleSide.RIGHT,
            shelf_idx=0,
        ),
    )


@pytest.mark.asyncio
async def test_ensure_session_started_rejects_reference_bounds_before_starting_background_task() -> None:
    manager = LaserAutoCalibrationManager()
    scan_config = _build_reference_bounds_scan_config(
        left_reference=SimpleNamespace(
            shelf_idx=0,
            cell_idx=0,
            anchor="top_left",
            x_mm=40.0,
            y_mm=100.0,
            z_offset_mm=15.0,
            wall_depth_mm=9.0,
        ),
    )
    config = SimpleNamespace(robot=SimpleNamespace(grid=SimpleNamespace(scan=scan_config)))

    with patch("openshelf_os_server.core.laser_auto_calibration.get_root_config", return_value=config):
        with pytest.raises(ValueError, match="xy_reference x_mm resolves outside scan X bounds"):
            await manager.ensure_session_started(
                SimpleNamespace(),
                LaserAutoCalibrationScope(mode="side", side=StorageModuleSide.LEFT),
            )

    assert manager._task is None


@pytest.mark.asyncio
async def test_home_after_abort_homes_tool_and_door() -> None:
    manager = LaserAutoCalibrationManager()
    tool = SimpleNamespace(home=AsyncMock(), _mx=SimpleNamespace(move_to_position=AsyncMock()))
    io = SimpleNamespace(home_door=AsyncMock())
    worker = SimpleNamespace(
        _robot=SimpleNamespace(_tool=tool, _io=io),
        clear_needs_home=MagicMock(),
        get_status_snapshot=AsyncMock(),
    )

    await manager._home_after_abort(worker)

    tool._mx.move_to_position.assert_awaited_once_with(angle_module.RIGHT_MODULE_ROTATION_SAFE_X_MM)
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
async def test_abort_returns_existing_cancelling_snapshot() -> None:
    manager = LaserAutoCalibrationManager()

    async with manager._lock:
        manager._task = asyncio.create_task(asyncio.sleep(10))
        manager._snapshot = LaserAutoCalibrationSnapshot(
            state='running',
            session_id='laser-session',
            current_stage='cancelling',
            updated_at='2026-01-01T00:00:00+00:00',
        )

    snapshot = await manager.abort()

    assert snapshot.current_stage == 'cancelling'


@pytest.mark.asyncio
async def test_run_session_cancel_marks_needs_home_without_abort_homing() -> None:
    manager = LaserAutoCalibrationManager()
    worker = SimpleNamespace(
        set_needs_home=MagicMock(),
        get_status_snapshot=AsyncMock(),
    )

    with patch.object(manager, '_get_current_calibration_params', AsyncMock(return_value={})):
        with patch.object(manager, '_run_all_sides', AsyncMock(side_effect=asyncio.CancelledError)):
            with patch.object(manager, '_restore_in_memory_calibration_params') as restore_mock:
                with patch.object(manager, '_set_terminal_state', AsyncMock()) as terminal_mock:
                    with patch.object(manager, '_home_after_abort', AsyncMock()) as home_mock:
                        await manager._run_session(worker, 'laser-session')

    restore_mock.assert_called_once_with(worker, {})
    worker.set_needs_home.assert_not_called()
    worker.get_status_snapshot.assert_not_awaited()
    terminal_mock.assert_awaited_once_with('cancelled', None)
    home_mock.assert_awaited_once_with(worker)


@pytest.mark.asyncio
async def test_scan_status_callback_does_not_overwrite_terminal_state(tmp_path: Path) -> None:
    manager = LaserAutoCalibrationManager()
    await manager._set_terminal_state("completed", None)
    callback = manager._build_scan_status_callback(
        "left_module",
        SimpleNamespace(
            run_id="run-id",
            metadata_path=tmp_path / "metadata.json",
            progress_path=tmp_path / "progress.json",
        ),
        ScanBounds(x_min=0.0, x_max=1.0, y_min=0.0, y_max=1.0),
    )

    await callback("z_offset_inference=applied")

    snapshot = await manager.get_snapshot()
    assert snapshot.state == "completed"
    assert snapshot.current_stage == "completed"

@pytest.mark.asyncio
async def test_run_angle_calibration_uses_stage_stall_method_when_configured() -> None:
    manager = LaserAutoCalibrationManager()
    manager._update_snapshot = AsyncMock()
    stage_motor = SimpleNamespace(
        _params=SimpleNamespace(lim=75.0, rpm=100),
        home=AsyncMock(),
    )
    tool = SimpleNamespace(
        _laser_enabled=True,
        _laser=object(),
        _mx=object(),
        _my=object(),
        _mg=object(),
        _ms=stage_motor,
        _mr=SimpleNamespace(_params=SimpleNamespace(lim=270.0)),
        _params=SimpleNamespace(
            parameters=SimpleNamespace(
                STAGE_EXTENSION_SHELF_FRONT=52.0,
                STAGE_EXTENSION_SHELF_BACK=37.0,
            )
        ),
        retract=AsyncMock(),
        get_param=AsyncMock(side_effect=[184.5, 160.5, 800.0, 1200.0]),
    )
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    scan_config = SimpleNamespace(
        angle_calibration_method='stage_stall',
        angle_calibration_stage_stall_rotation_min_deg=-1.0,
        angle_calibration_stage_stall_rotation_max_deg=1.0,
        angle_calibration_rotation_step_deg=1.0,
        angle_calibration_stage_stall_probe_x_mm=475.0,
        angle_calibration_stage_stall_start_mm=3.0,
        angle_calibration_stage_stall_target_mm=None,
        angle_calibration_stage_stall_probe_step_mm=0.5,
        angle_calibration_stage_stall_probe_rpm=20,
        angle_calibration_stage_stall_fast_approach_margin_mm=2.0,
        angle_calibration_stage_stall_fast_approach_rpm=20,
        angle_calibration_stage_stall_timeout_s=30.0,
        angle_calibration_stage_stall_no_progress_timeout_s=0.25,
        angle_calibration_stage_stall_backoff_mm=8.0,
        angle_calibration_stage_stall_fall_threshold_mm=0.15,
        angle_calibration_stage_stall_stop_after_fall_count=1,
        angle_calibration_stage_stall_fine_window_deg=0.0,
        angle_calibration_stage_stall_fine_step_deg=0.25,
        laser_sample_settle_ms=0,
        laser_samples_per_point=1,
        laser_scan_z_offset_from_place_mm=0.0,
        laser_scan_stage_extension_mm=60.0,
    )
    config = SimpleNamespace(robot=SimpleNamespace(grid=SimpleNamespace(scan=scan_config)))

    with patch('openshelf_os_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'resolve_capture_y_mm', AsyncMock(return_value=1144.0)):
            with patch.object(angle_module, 'prepare_capture_pose', AsyncMock()) as prepare_mock:
                with patch.object(
                    angle_module,
                    'probe_stage_until_stall',
                    AsyncMock(
                        side_effect=[
                            (True, 60.27, 'no_progress', 0),
                            (True, 61.655, 'no_progress', 0),
                            (True, 60.005, 'no_progress', 0),
                        ]
                    ),
                ) as probe_mock:
                    with patch.object(angle_module, 'retract_stage_after_stall', AsyncMock()):
                        with patch.object(angle_module, 'finish_stage_stall_capture', AsyncMock()):
                            result = await manager._run_angle_calibration_for_side(
                                worker,
                                StorageModuleSide.LEFT,
                                ScanBounds(x_min=5.0, x_max=950.0, y_min=1000.0, y_max=1210.0),
                            )

    assert result.method == 'stage_stall'
    assert result.previous_rotation_deg == pytest.approx(184.5)
    assert result.updated_rotation_deg == pytest.approx(184.5)
    assert result.alpha_deg == pytest.approx(0.0)
    assert result.stage_position_mm == pytest.approx(61.655)
    assert result.sample_count == 3
    assert stage_motor.home.await_count == 2
    assert prepare_mock.await_count == 3
    assert probe_mock.await_args.kwargs['fast_approach_mm'] == pytest.approx(3.0)
    assert probe_mock.await_args.kwargs['should_use_continuous_move'] is True


@pytest.mark.asyncio
async def test_run_angle_calibration_validates_top_fine_angles_by_median() -> None:
    manager = LaserAutoCalibrationManager()
    manager._update_snapshot = AsyncMock()
    stage_motor = SimpleNamespace(
        _params=SimpleNamespace(lim=75.0, rpm=100),
        home=AsyncMock(),
    )
    tool = SimpleNamespace(
        _laser_enabled=True,
        _laser=object(),
        _mx=object(),
        _my=object(),
        _mg=object(),
        _ms=stage_motor,
        _mr=SimpleNamespace(_params=SimpleNamespace(lim=270.0)),
        _params=SimpleNamespace(
            parameters=SimpleNamespace(
                STAGE_EXTENSION_SHELF_FRONT=52.0,
                STAGE_EXTENSION_SHELF_BACK=37.0,
            )
        ),
        retract=AsyncMock(),
        get_param=AsyncMock(side_effect=[4.5, 160.5, 800.0, 1200.0]),
    )
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    scan_config = SimpleNamespace(
        angle_calibration_method='stage_stall',
        angle_calibration_stage_stall_rotation_min_deg=0.0,
        angle_calibration_stage_stall_rotation_max_deg=1.0,
        angle_calibration_rotation_step_deg=1.0,
        angle_calibration_stage_stall_coarse_start_deg=None,
        angle_calibration_stage_stall_probe_x_mm=475.0,
        angle_calibration_stage_stall_start_mm=3.0,
        angle_calibration_stage_stall_target_mm=None,
        angle_calibration_stage_stall_probe_step_mm=0.5,
        angle_calibration_stage_stall_probe_rpm=20,
        angle_calibration_stage_stall_fast_approach_mm=20.0,
        angle_calibration_stage_stall_fast_approach_margin_mm=2.0,
        angle_calibration_stage_stall_fast_approach_rpm=20,
        angle_calibration_stage_stall_timeout_s=30.0,
        angle_calibration_stage_stall_no_progress_timeout_s=0.25,
        angle_calibration_stage_stall_backoff_mm=8.0,
        angle_calibration_stage_stall_fine_window_deg=0.25,
        angle_calibration_stage_stall_fine_step_deg=0.25,
        angle_calibration_stage_stall_validation_count=2,
        angle_calibration_stage_stall_validation_repeats=2,
        angle_calibration_stage_stall_validation_retry_count=0,
        angle_calibration_stage_stall_min_valid_stage_position_mm=0.0,
        angle_calibration_stage_stall_validation_plateau_tolerance_mm=0.0,
        angle_calibration_stage_stall_coarse_validation_count=0,
        angle_calibration_stage_stall_validation_high_side_window_deg=0.0,
        angle_calibration_stage_stall_low_contact_retry_count=0,
        angle_calibration_stage_stall_validation_min_valid_sample_count=2,
        laser_sample_settle_ms=0,
        laser_samples_per_point=1,
        laser_scan_z_offset_from_place_mm=0.0,
        laser_scan_stage_extension_mm=60.0,
    )
    config = SimpleNamespace(robot=SimpleNamespace(grid=SimpleNamespace(scan=scan_config)))
    probe_results = [
        (True, 20.0, 'no_progress', 0),
        (True, 25.0, 'no_progress', 0),
        (True, 30.0, 'no_progress', 0),
        (True, 31.0, 'no_progress', 0),
        (True, 20.0, 'no_progress', 0),
        (True, 28.0, 'no_progress', 0),
        (True, 29.0, 'no_progress', 0),
        (True, 32.0, 'no_progress', 0),
        (True, 33.0, 'no_progress', 0),
    ]

    with patch('openshelf_os_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'resolve_capture_y_mm', AsyncMock(return_value=1144.0)):
            with patch.object(angle_module, 'prepare_capture_pose', AsyncMock()):
                with patch.object(angle_module, 'probe_stage_until_stall', AsyncMock(side_effect=probe_results)):
                    with patch.object(angle_module, 'retract_stage_after_stall', AsyncMock()):
                        with patch.object(angle_module, 'finish_stage_stall_capture', AsyncMock()):
                            with patch.object(angle_module, 'write_stage_stall_output_document') as write_mock:
                                result = await manager._run_angle_calibration_for_side(
                                    worker,
                                    StorageModuleSide.RIGHT,
                                    ScanBounds(x_min=5.0, x_max=950.0, y_min=1000.0, y_max=1210.0),
                                )

    assert result.updated_rotation_deg == pytest.approx(5.25)
    assert result.alpha_deg == pytest.approx(0.75)
    assert result.stage_position_mm == pytest.approx(32.5)
    assert result.sample_count == 9
    assert write_mock.call_args.kwargs['metadata']['selected_pass'] == 'validation'


@pytest.mark.asyncio
async def test_run_angle_calibration_retries_low_validation_contacts() -> None:
    manager = LaserAutoCalibrationManager()
    manager._update_snapshot = AsyncMock()
    stage_motor = SimpleNamespace(
        _params=SimpleNamespace(lim=75.0, rpm=100),
        home=AsyncMock(),
    )
    tool = SimpleNamespace(
        _laser_enabled=True,
        _laser=object(),
        _mx=object(),
        _my=object(),
        _mg=object(),
        _ms=stage_motor,
        _mr=SimpleNamespace(_params=SimpleNamespace(lim=270.0)),
        _params=SimpleNamespace(
            parameters=SimpleNamespace(
                STAGE_EXTENSION_SHELF_FRONT=52.0,
                STAGE_EXTENSION_SHELF_BACK=37.0,
            )
        ),
        retract=AsyncMock(),
        get_param=AsyncMock(side_effect=[4.5, 160.5, 800.0, 1200.0]),
    )
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    scan_config = SimpleNamespace(
        angle_calibration_method='stage_stall',
        angle_calibration_stage_stall_rotation_min_deg=0.0,
        angle_calibration_stage_stall_rotation_max_deg=1.0,
        angle_calibration_rotation_step_deg=1.0,
        angle_calibration_stage_stall_coarse_start_deg=None,
        angle_calibration_stage_stall_probe_x_mm=475.0,
        angle_calibration_stage_stall_start_mm=3.0,
        angle_calibration_stage_stall_target_mm=None,
        angle_calibration_stage_stall_probe_step_mm=0.5,
        angle_calibration_stage_stall_probe_rpm=20,
        angle_calibration_stage_stall_fast_approach_mm=20.0,
        angle_calibration_stage_stall_fast_approach_margin_mm=2.0,
        angle_calibration_stage_stall_fast_approach_rpm=20,
        angle_calibration_stage_stall_timeout_s=30.0,
        angle_calibration_stage_stall_no_progress_timeout_s=0.25,
        angle_calibration_stage_stall_backoff_mm=8.0,
        angle_calibration_stage_stall_fine_window_deg=0.25,
        angle_calibration_stage_stall_fine_step_deg=0.25,
        angle_calibration_stage_stall_validation_count=2,
        angle_calibration_stage_stall_validation_repeats=2,
        angle_calibration_stage_stall_validation_retry_count=1,
        angle_calibration_stage_stall_min_valid_stage_position_mm=35.0,
        angle_calibration_stage_stall_validation_plateau_tolerance_mm=0.5,
        angle_calibration_stage_stall_coarse_validation_count=0,
        angle_calibration_stage_stall_validation_high_side_window_deg=0.0,
        angle_calibration_stage_stall_low_contact_retry_count=0,
        angle_calibration_stage_stall_validation_min_valid_sample_count=2,
        laser_sample_settle_ms=0,
        laser_samples_per_point=1,
        laser_scan_z_offset_from_place_mm=0.0,
        laser_scan_stage_extension_mm=60.0,
    )
    config = SimpleNamespace(robot=SimpleNamespace(grid=SimpleNamespace(scan=scan_config)))
    probe_results = [
        (True, 20.0, 'no_progress', 0),
        (True, 40.0, 'no_progress', 0),
        (True, 41.9, 'no_progress', 0),
        (True, 42.2, 'no_progress', 0),
        (True, 41.8, 'no_progress', 0),
        (True, 29.0, 'no_progress', 0),
        (True, 29.0, 'no_progress', 0),
        (True, 29.0, 'no_progress', 0),
        (True, 29.0, 'no_progress', 0),
        (True, 42.2, 'no_progress', 0),
        (True, 42.1, 'no_progress', 0),
        (True, 41.9, 'no_progress', 0),
        (True, 41.8, 'no_progress', 0),
    ]

    with patch('openshelf_os_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'resolve_capture_y_mm', AsyncMock(return_value=1144.0)):
            with patch.object(angle_module, 'prepare_capture_pose', AsyncMock()):
                with patch.object(angle_module, 'probe_stage_until_stall', AsyncMock(side_effect=probe_results)):
                    with patch.object(angle_module, 'retract_stage_after_stall', AsyncMock()):
                        with patch.object(angle_module, 'finish_stage_stall_capture', AsyncMock()):
                            with patch.object(angle_module, 'write_stage_stall_output_document') as write_mock:
                                result = await manager._run_angle_calibration_for_side(
                                    worker,
                                    StorageModuleSide.RIGHT,
                                    ScanBounds(x_min=5.0, x_max=950.0, y_min=1000.0, y_max=1210.0),
                                )

    assert result.updated_rotation_deg == pytest.approx(5.5)
    assert result.stage_position_mm == pytest.approx(42.15)
    assert result.sample_count == 13
    assert write_mock.call_args.kwargs['metadata']['selected_pass'] == 'validation'
@pytest.mark.asyncio
async def test_run_angle_calibration_skips_final_retract_when_cancelled() -> None:
    manager = LaserAutoCalibrationManager()
    started = asyncio.Event()
    tool = SimpleNamespace(
        _laser_enabled=True,
        _laser=object(),
        _mx=object(),
        _my=object(),
        _mg=object(),
        _ms=SimpleNamespace(_params=SimpleNamespace(lim=100.0)),
        _mr=SimpleNamespace(_params=SimpleNamespace(lim=360.0)),
        _params=SimpleNamespace(parameters=SimpleNamespace()),
        retract=AsyncMock(),
        get_param=AsyncMock(side_effect=[3.815, 100.0, 200.0, 200.0]),
    )
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    scan_config = SimpleNamespace(
        laser_sample_settle_ms=0,
        laser_samples_per_point=1,
        laser_scan_stage_extension_mm=None,
        laser_scan_z_offset_from_place_mm=None,
        angle_calibration_x_min_mm=50.0,
        angle_calibration_x_max_mm=940.0,
        angle_calibration_x_step_mm=100.0,
        angle_calibration_x_count=1,
        angle_calibration_z_offset_min_mm=0.0,
        angle_calibration_z_offset_max_mm=0.0,
        angle_calibration_z_offset_step_mm=1.0,
        angle_calibration_z_offset_count=1,
        angle_calibration_rotation_min_deg=0.0,
        angle_calibration_rotation_max_deg=0.0,
        angle_calibration_rotation_step_deg=1.0,
    )
    config = SimpleNamespace(robot=SimpleNamespace(grid=SimpleNamespace(scan=scan_config)))

    async def did_resolve_capture_y_mm(*args, **kwargs) -> float:
        started.set()
        await asyncio.sleep(10)
        return 1095.0

    with patch('openshelf_os_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'resolve_capture_y_mm', side_effect=did_resolve_capture_y_mm):
            task = asyncio.create_task(
                manager._run_angle_calibration_for_side(
                    worker,
                    StorageModuleSide.LEFT,
                    ScanBounds(x_min=5.0, x_max=950.0, y_min=0.0, y_max=1250.0),
                )
            )

            await started.wait()
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

    assert tool.retract.await_count == 1


@pytest.mark.asyncio
async def test_run_angle_calibration_raises_when_abort_requested_during_capture() -> None:
    manager = LaserAutoCalibrationManager()
    tool = SimpleNamespace(
        _laser_enabled=True,
        _laser=object(),
        _mx=object(),
        _my=object(),
        _mg=object(),
        _ms=SimpleNamespace(_params=SimpleNamespace(lim=100.0)),
        _mr=SimpleNamespace(_params=SimpleNamespace(lim=360.0)),
        _params=SimpleNamespace(parameters=SimpleNamespace()),
        retract=AsyncMock(),
        get_param=AsyncMock(side_effect=[3.815, 100.0, 200.0, 200.0]),
    )
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    scan_config = SimpleNamespace(
        laser_sample_settle_ms=0,
        laser_samples_per_point=1,
        laser_scan_stage_extension_mm=None,
        laser_scan_z_offset_from_place_mm=None,
        angle_calibration_x_min_mm=50.0,
        angle_calibration_x_max_mm=940.0,
        angle_calibration_x_step_mm=100.0,
        angle_calibration_x_count=1,
        angle_calibration_z_offset_min_mm=0.0,
        angle_calibration_z_offset_max_mm=0.0,
        angle_calibration_z_offset_step_mm=1.0,
        angle_calibration_z_offset_count=1,
        angle_calibration_rotation_min_deg=0.0,
        angle_calibration_rotation_max_deg=0.0,
        angle_calibration_rotation_step_deg=1.0,
    )
    config = SimpleNamespace(robot=SimpleNamespace(grid=SimpleNamespace(scan=scan_config)))

    async def did_prepare_capture_pose(*args, **kwargs) -> None:
        manager._abort_requested = True

    with patch('openshelf_os_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'resolve_capture_y_mm', AsyncMock(return_value=1095.0)):
            with patch.object(angle_module, 'prepare_capture_pose', side_effect=did_prepare_capture_pose):
                with patch.object(angle_module, 'sample_laser_depth', AsyncMock(return_value=12.0)):
                    with pytest.raises(asyncio.CancelledError):
                        await manager._run_angle_calibration_for_side(
                            worker,
                            StorageModuleSide.LEFT,
                            ScanBounds(x_min=5.0, x_max=950.0, y_min=0.0, y_max=1250.0),
                        )


@pytest.mark.asyncio
async def test_scan_status_callback_preserves_cancelling_snapshot() -> None:
    manager = LaserAutoCalibrationManager()
    manager._abort_requested = True
    manager._snapshot = LaserAutoCalibrationSnapshot(
        state='running',
        session_id='laser-session',
        current_side='left_module',
        current_stage='cancelling',
        updated_at='2026-01-01T00:00:00+00:00',
    )
    manager._refresh_scan_side_state = AsyncMock()
    manager._update_snapshot = AsyncMock()
    callback = manager._build_scan_status_callback(
        'left_module',
        SimpleNamespace(run_id='run-id'),
        ScanBounds(x_min=0.0, x_max=1.0, y_min=0.0, y_max=1.0),
    )

    await callback('phase=shelf-scout scout=1/1 x=950.00')

    manager._refresh_scan_side_state.assert_not_awaited()
    manager._update_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_all_sides_processes_right_before_left() -> None:
    manager = LaserAutoCalibrationManager()
    tool = SimpleNamespace()
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
    right_angle_result = LaserAutoCalibrationAngleResult(
        side='right_module',
        alpha_deg=11.25,
        sample_count=45,
        capture_y_mm=1140.0,
        rms_residual_mm=0.12,
        max_abs_residual_mm=0.24,
        line_angle_deg=90.0,
        previous_rotation_deg=180.0,
        updated_rotation_deg=191.25,
    )

    async def did_run_side(current_worker, side: StorageModuleSide, angle_result: LaserAutoCalibrationAngleResult):
        assert current_worker is worker
        processed_sides.append(side)

        return SimpleNamespace(side=side, angle_result=angle_result)

    with patch('openshelf_os_server.core.laser_auto_calibration._build_bounds_for_side', return_value=ScanBounds(x_min=0.0, x_max=1.0, y_min=0.0, y_max=1.0)):
        with patch.object(manager, '_run_angle_calibration_for_side', AsyncMock(side_effect=[right_angle_result, left_angle_result])) as run_angle_mock:
            with patch.object(manager, '_apply_angle_calibration_in_memory', AsyncMock()) as apply_mock:
                with patch.object(manager, '_resolve_stage_extension_probe_y_mm', AsyncMock(side_effect=[1148.0, 1120.0])) as probe_y_mock:
                    with patch.object(manager, '_run_stage_extension_calibration_for_side', AsyncMock(side_effect=[44.0, 41.0])) as stage_mock:
                        with patch.object(manager, '_apply_stage_extension_calibration_in_memory', AsyncMock()) as apply_stage_mock:
                            with patch.object(manager, '_run_side', side_effect=did_run_side):
                                with patch.object(manager, '_persist_completed_results', AsyncMock()) as persist_mock:
                                    await manager._run_all_sides(worker)

    assert processed_sides == [StorageModuleSide.RIGHT, StorageModuleSide.LEFT]
    assert run_angle_mock.await_count == 2
    assert apply_mock.await_count == 2
    assert stage_mock.await_count == 2
    assert apply_stage_mock.await_count == 2
    persist_mock.assert_awaited_once()
    assert probe_y_mock.await_count == 2

    assert run_angle_mock.await_args_list[0].args[1] == StorageModuleSide.RIGHT
    assert run_angle_mock.await_args_list[1].args[1] == StorageModuleSide.LEFT
    assert stage_mock.await_args_list[0].args[1] == StorageModuleSide.RIGHT
    assert stage_mock.await_args_list[1].args[1] == StorageModuleSide.LEFT
    assert stage_mock.await_args_list[0].kwargs['probe_y_mm'] == pytest.approx(1148.0)
    assert stage_mock.await_args_list[1].kwargs['probe_y_mm'] == pytest.approx(1120.0)
    assert probe_y_mock.await_args_list[0].args[1] == StorageModuleSide.RIGHT
    assert probe_y_mock.await_args_list[1].args[1] == StorageModuleSide.LEFT
    assert probe_y_mock.await_args_list[0].kwargs['top_shelf_y_mm'] == pytest.approx(right_angle_result.capture_y_mm)
    assert probe_y_mock.await_args_list[1].kwargs['top_shelf_y_mm'] == pytest.approx(left_angle_result.capture_y_mm)

    persisted_results = persist_mock.await_args.args[1]

    assert [result.side for result in persisted_results] == [StorageModuleSide.RIGHT, StorageModuleSide.LEFT]
    assert persisted_results[0].angle_result.updated_rotation_deg == pytest.approx(191.25)


@pytest.mark.asyncio
async def test_run_requested_scope_both_runs_full_two_side_scan() -> None:
    manager = LaserAutoCalibrationManager()
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=SimpleNamespace()))
    requested_scope = LaserAutoCalibrationScope(
        mode="both",
        include_io_box=False,
        include_angle_calibration=False,
        include_extension_calibration=True,
    )

    with patch(
        'openshelf_os_server.core.laser_auto_calibration._build_bounds_for_side',
        return_value=ScanBounds(x_min=0.0, x_max=950.0, y_min=0.0, y_max=1210.0),
    ):
        with patch.object(manager, '_run_all_sides', AsyncMock()) as run_all_mock:
            with patch.object(manager, '_run_io_box_calibration', AsyncMock()) as io_box_mock:
                await manager._run_requested_scope(worker, requested_scope)

    run_all_mock.assert_awaited_once()
    assert run_all_mock.await_args.args[:3] == (worker, False, True)
    io_box_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_single_side_uses_angle_capture_y_for_stage_probe() -> None:
    manager = LaserAutoCalibrationManager()
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=SimpleNamespace()))
    angle_result = LaserAutoCalibrationAngleResult(
        side='right_module',
        alpha_deg=5.5,
        sample_count=45,
        capture_y_mm=1114.0,
        rms_residual_mm=0.12,
        max_abs_residual_mm=0.24,
        line_angle_deg=90.0,
        previous_rotation_deg=180.0,
        updated_rotation_deg=185.5,
    )

    with patch('openshelf_os_server.core.laser_auto_calibration._build_bounds_for_side', return_value=ScanBounds(x_min=0.0, x_max=950.0, y_min=0.0, y_max=1210.0)):
        with patch.object(manager, '_run_angle_calibration_for_side', AsyncMock(return_value=angle_result)):
            with patch.object(manager, '_apply_angle_calibration_in_memory', AsyncMock()):
                with patch.object(manager, '_resolve_stage_extension_probe_y_mm', AsyncMock(return_value=1139.0)) as probe_y_mock:
                    with patch.object(manager, '_run_stage_extension_calibration_for_side', AsyncMock(return_value=37.0)):
                        with patch.object(manager, '_apply_stage_extension_calibration_in_memory', AsyncMock()):
                            with patch.object(manager, '_run_side', AsyncMock(return_value=SimpleNamespace(side=StorageModuleSide.RIGHT, angle_result=angle_result))):
                                with patch.object(manager, '_persist_completed_results', AsyncMock()):
                                    await manager._run_single_side(worker, StorageModuleSide.RIGHT)

    assert probe_y_mock.await_args.kwargs['top_shelf_y_mm'] == pytest.approx(angle_result.capture_y_mm)



@pytest.mark.asyncio
async def test_run_single_side_skips_extension_calibration_when_disabled() -> None:
    manager = LaserAutoCalibrationManager()
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=SimpleNamespace()))
    angle_result = LaserAutoCalibrationAngleResult(
        side='right_module',
        alpha_deg=5.5,
        sample_count=45,
        capture_y_mm=1114.0,
        rms_residual_mm=0.12,
        max_abs_residual_mm=0.24,
        line_angle_deg=90.0,
        previous_rotation_deg=180.0,
        updated_rotation_deg=185.5,
    )

    with patch('openshelf_os_server.core.laser_auto_calibration._build_bounds_for_side', return_value=ScanBounds(x_min=0.0, x_max=950.0, y_min=0.0, y_max=1210.0)):
        with patch.object(manager, '_run_angle_calibration_for_side', AsyncMock(return_value=angle_result)):
            with patch.object(manager, '_apply_angle_calibration_in_memory', AsyncMock()):
                with patch.object(manager, '_resolve_stage_extension_probe_y_mm', AsyncMock()) as probe_y_mock:
                    with patch.object(manager, '_run_stage_extension_calibration_for_side', AsyncMock()) as stage_mock:
                        with patch.object(manager, '_apply_stage_extension_calibration_in_memory', AsyncMock()) as apply_stage_mock:
                            with patch.object(manager, '_run_side', AsyncMock(return_value=SimpleNamespace(side=StorageModuleSide.RIGHT, angle_result=angle_result))):
                                with patch.object(manager, '_persist_completed_results', AsyncMock()) as persist_mock:
                                    await manager._run_single_side(
                                        worker,
                                        StorageModuleSide.RIGHT,
                                        include_extension_calibration=False,
                                    )

    probe_y_mock.assert_not_awaited()
    stage_mock.assert_not_awaited()
    apply_stage_mock.assert_not_awaited()
    persist_mock.assert_awaited_once()

@pytest.mark.asyncio
async def test_run_angle_calibration_uses_stage_stall_only() -> None:
    manager = LaserAutoCalibrationManager()
    expected = LaserAutoCalibrationAngleResult(
        method='stage_stall',
        side=StorageModuleSide.LEFT,
        alpha_deg=1.5,
        sample_count=2,
        capture_y_mm=100.0,
        rms_residual_mm=0.0,
        max_abs_residual_mm=0.0,
        line_angle_deg=1.5,
        previous_rotation_deg=10.0,
        updated_rotation_deg=11.5,
        stage_position_mm=42.0,
    )
    worker = SimpleNamespace()
    bounds = ScanBounds(x_min=5.0, x_max=950.0, y_min=0.0, y_max=1250.0)

    with patch.object(
        manager,
        '_run_stage_stall_angle_calibration_for_side',
        AsyncMock(return_value=expected),
    ) as stage_stall_mock:
        result = await manager._run_angle_calibration_for_side(
            worker,
            StorageModuleSide.LEFT,
            bounds,
        )

    assert result == expected
    stage_stall_mock.assert_awaited_once_with(worker, StorageModuleSide.LEFT, bounds)

@pytest.mark.asyncio
async def test_persist_completed_results_saves_stage_extensions() -> None:
    manager = LaserAutoCalibrationManager()
    manager._side_states['left_module'] = _SideState(stage_extension_mm=41.0)
    manager._side_states['right_module'] = _SideState(stage_extension_mm=44.0)

    params = Parameters.model_validate(
        {
            'IDLE_POSITION': [0, 0],
            'PICKUP_POSITION_L': [0, 0],
            'PICKUP_POSITION_H': [0, 0],
            'SCAN_POSITION': [0, 0],
            'STAGE_EXTENSION_IO_BOX': 0,
            'STAGE_NUDGE': 0,
            'STAGE_EXTENSION_SHELF_FRONT': 0,
            'STAGE_EXTENSION_SHELF_BACK': 0,
            'Z_EXTENSION_PICK': 0,
            'Z_EXTENSION_PLACE': 0,
            'Z_EXTENSION_IO': 0,
            'Z_MOVE_RPM': 1,
            'Z_MOVE_ACCEL': 1,
            'ROTATION_FRONT': 0,
            'ROTATION_BACK': 180,
        }
    )
    scan_config = SimpleNamespace()
    config = SimpleNamespace(
        robot=SimpleNamespace(
            tool=SimpleNamespace(parameters=params),
            grid=SimpleNamespace(path='storage.json', scan=scan_config),
        )
    )
    config.model_copy = lambda deep=True: copy.deepcopy(config)

    source_grid = SimpleNamespace(left_module=[], right_module=[], filename='storage.json', save=MagicMock())
    reloaded_grid = SimpleNamespace()
    saved_configs: list[object] = []
    saved_storage_grids: list[object] = []

    class FakeStorageGrid:
        def __init__(self, *, left_module, right_module, filename) -> None:
            self.left_module = left_module
            self.right_module = right_module
            self.filename = filename

        def save(self) -> None:
            saved_storage_grids.append(self)

    worker = SimpleNamespace(_robot=SimpleNamespace(reload_parameters=MagicMock(), _grid=None))
    analysis = SimpleNamespace(shelves=[object()], metadata_path=Path('/tmp/fake-metadata.json'))
    completed_results = [
        SimpleNamespace(
            side=StorageModuleSide.LEFT,
            angle_result=LaserAutoCalibrationAngleResult(
                side='left_module',
                alpha_deg=1.0,
                sample_count=1,
                capture_y_mm=100.0,
                rms_residual_mm=0.1,
                max_abs_residual_mm=0.2,
                line_angle_deg=90.0,
                previous_rotation_deg=0.0,
                updated_rotation_deg=4.0,
            ),
            analysis=analysis,
        ),
        SimpleNamespace(
            side=StorageModuleSide.RIGHT,
            angle_result=LaserAutoCalibrationAngleResult(
                side='right_module',
                alpha_deg=1.0,
                sample_count=1,
                capture_y_mm=100.0,
                rms_residual_mm=0.1,
                max_abs_residual_mm=0.2,
                line_angle_deg=90.0,
                previous_rotation_deg=180.0,
                updated_rotation_deg=184.0,
            ),
            analysis=analysis,
        ),
    ]

    with patch('openshelf_os_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch('openshelf_os_server.core.laser_auto_calibration.resolve_grid_path', return_value='storage.json'):
            with patch('openshelf_os_server.core.laser_auto_calibration.laser_module.load_grid', side_effect=[source_grid, reloaded_grid]):
                with patch('openshelf_os_server.core.laser_auto_calibration.laser_module.build_shelves_from_scan', side_effect=[[object()], [object()]]):
                    with patch('openshelf_os_server.core.laser_auto_calibration.laser_module.apply_xy_reference_offsets'):
                        with patch('openshelf_os_server.core.laser_auto_calibration.laser_module.apply_wall_depth_z_offsets'):
                            with patch('openshelf_os_server.core.laser_auto_calibration.laser_module.StorageGrid', FakeStorageGrid):
                                with patch('openshelf_os_server.core.laser_auto_calibration.save_root_config', side_effect=lambda next_config: saved_configs.append(next_config)):
                                    with patch('openshelf_os_server.core.laser_auto_calibration.clear_config_cache'):
                                        await manager._persist_completed_results(worker, completed_results)

    assert len(saved_storage_grids) == 1
    assert len(saved_configs) == 1
    assert saved_configs[0].robot.tool.parameters.ROTATION_FRONT == pytest.approx(4.0)
    assert saved_configs[0].robot.tool.parameters.ROTATION_BACK == pytest.approx(184.0)
    assert saved_configs[0].robot.tool.parameters.STAGE_EXTENSION_SHELF_FRONT == pytest.approx(41.0)
    assert saved_configs[0].robot.tool.parameters.STAGE_EXTENSION_SHELF_BACK == pytest.approx(44.0)
    worker._robot.reload_parameters.assert_called_once_with()
    assert worker._robot._grid is reloaded_grid



@pytest.mark.asyncio
async def test_run_single_shelf_uses_known_shelf_edge_with_neighbor_clipped_bounds() -> None:
    manager = LaserAutoCalibrationManager()
    manager._update_snapshot = AsyncMock()
    scan_config = SimpleNamespace(
        right_bounds_x_min_mm=0.0,
        right_bounds_x_max_mm=950.0,
        right_bounds_y_min_mm=0.0,
        right_bounds_y_max_mm=1250.0,
        expected_hole_height_mm=30.0,
        laser_offset_y_mm=0.0,
        shelf_hole_probe_y_offsets_mm=[32.25],
        right_xy_reference=SimpleNamespace(
            shelf_idx=6,
            cell_idx=0,
            anchor='top_left',
            x_mm=8.0,
            y_mm=1162.5,
        ),
        right_z_reference=SimpleNamespace(
            shelf_idx=6,
            cell_idx=0,
            z_offset_mm=9.0,
            stage_extension_mm=47.0,
        ),
    )
    config = SimpleNamespace(robot=SimpleNamespace(grid=SimpleNamespace(scan=scan_config)))
    target_shelf = SimpleNamespace(
        shelf_idx=0,
        cells=[
            SimpleNamespace(calibration_point=SimpleNamespace(y=62.0)),
            SimpleNamespace(calibration_point=SimpleNamespace(y=63.0)),
        ],
    )
    next_shelf = SimpleNamespace(
        shelf_idx=1,
        cells=[
            SimpleNamespace(calibration_point=SimpleNamespace(y=162.0)),
            SimpleNamespace(calibration_point=SimpleNamespace(y=163.0)),
        ],
    )
    grid = SimpleNamespace(
        get_shelf=lambda side, shelf_idx: target_shelf,
        get_side=lambda side: [target_shelf, next_shelf],
    )
    analysis = SimpleNamespace(shelves=[SimpleNamespace(cells=[])])
    xy_reference_detected_cell = ScanDetectedCell(
        shelf_idx=0,
        cell_idx=1,
        center_x_mm=10.0,
        center_y_mm=20.0,
        tool_x_mm=11.0,
        tool_y_mm=21.0,
        width_mm=30.0,
        height_mm=40.0,
        point_count=5,
        bbox_x_min=1.0,
        bbox_x_max=2.0,
        bbox_y_min=3.0,
        bbox_y_max=4.0,
    )
    worker = SimpleNamespace()

    with patch('openshelf_os_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch('openshelf_os_server.core.laser_auto_calibration.resolve_grid_path', return_value='storage.json'):
            with patch('openshelf_os_server.core.laser_auto_calibration.laser_module.load_grid', return_value=grid):
                with patch(
                    'openshelf_os_server.core.laser_auto_calibration.laser_module.build_single_shelf_known_edge_y',
                    return_value=[30.75],
                ) as known_edge_mock:
                    with patch.object(
                        manager,
                        '_run_partial_reference_scans',
                        AsyncMock(return_value=(xy_reference_detected_cell, 8.0)),
                    ):
                        with patch(
                            'openshelf_os_server.core.laser_auto_calibration.scope_runner._build_reference_apply_metadata',
                            return_value={'reference_apply': True},
                        ):
                            with patch.object(manager, '_run_scan_analysis', AsyncMock(return_value=analysis)) as scan_mock:
                                with patch.object(manager, '_persist_single_shelf_result', AsyncMock()) as persist_mock:
                                    await manager._run_single_shelf(worker, StorageModuleSide.RIGHT, 0)

    known_edge_mock.assert_called_once()
    assert known_edge_mock.call_args.kwargs['grid'] is grid
    assert known_edge_mock.call_args.kwargs['side'] == StorageModuleSide.RIGHT
    assert known_edge_mock.call_args.kwargs['shelf_idx'] == 0
    assert scan_mock.await_args.kwargs['known_shelf_edge_y'] == [30.75]
    scan_config_override = scan_mock.await_args.kwargs['scan_config_override']
    assert known_edge_mock.call_args.kwargs['scan_config'] is scan_config_override
    assert scan_config_override.shelf_hole_probe_y_offsets_mm == [pytest.approx(12.25)]
    assert scan_config.shelf_hole_probe_y_offsets_mm == [pytest.approx(32.25)]
    assert scan_mock.await_args.kwargs['should_validate_reference_bounds'] is False
    assert scan_mock.call_args.args[2].y_min == pytest.approx(0.0)
    assert scan_mock.call_args.args[2].y_max == pytest.approx(112.5)
    persist_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_single_cell_uses_seeded_cell_rescan_and_reference_offsets() -> None:
    manager = LaserAutoCalibrationManager()
    analysis = SimpleNamespace(
        shelves=[SimpleNamespace(cells=[])],
        wall_depth_mm=8.0,
    )
    candidate = laser_module.GapCandidateRegion(
        x_min=1.0,
        x_max=2.0,
        y_min=3.0,
        y_max=4.0,
        wall_depth_mm=8.0,
    )
    detected_cell = SimpleNamespace(tool_x_mm=10.0, tool_y_mm=20.0)
    xy_reference_detected_cell = ScanDetectedCell(
        shelf_idx=0,
        cell_idx=1,
        center_x_mm=10.0,
        center_y_mm=20.0,
        tool_x_mm=11.0,
        tool_y_mm=21.0,
        width_mm=30.0,
        height_mm=40.0,
        point_count=5,
        bbox_x_min=1.0,
        bbox_x_max=2.0,
        bbox_y_min=3.0,
        bbox_y_max=4.0,
    )
    target = laser_module.SingleCellRescanTarget(shelf_idx=1, cell_idx=2)
    worker = SimpleNamespace()

    with patch.object(
        manager,
        '_run_reference_cell_scan',
        AsyncMock(return_value=(analysis, candidate, detected_cell)),
    ) as target_scan_mock:
        with patch.object(
            manager,
            '_run_partial_reference_scans',
            AsyncMock(return_value=(xy_reference_detected_cell, 8.0)),
        ) as reference_scan_mock:
            with patch.object(manager, '_persist_single_cell_result', AsyncMock()) as persist_mock:
                with patch(
                    'openshelf_os_server.core.laser_auto_calibration.scope_runner._persist_reference_apply_metadata'
                ) as persist_reference_mock:
                    with patch(
                        'openshelf_os_server.core.laser_auto_calibration.scope_runner._get_xy_reference_target',
                        return_value=laser_module.SingleCellRescanTarget(shelf_idx=0, cell_idx=1),
                    ):
                        with patch(
                            'openshelf_os_server.core.laser_auto_calibration.scope_runner._get_z_reference_target',
                            return_value=laser_module.SingleCellRescanTarget(shelf_idx=0, cell_idx=1),
                        ):
                            await manager._run_single_cell(
                                worker,
                                StorageModuleSide.LEFT,
                                target,
                            )

    target_scan_mock.assert_awaited_once_with(worker, StorageModuleSide.LEFT, target)
    reference_scan_mock.assert_awaited_once_with(
        worker,
        StorageModuleSide.LEFT,
        reuse_target=target,
        reuse_analysis=analysis,
        reuse_candidate=candidate,
        reuse_detected_cell=detected_cell,
    )
    persist_mock.assert_awaited_once_with(
        worker,
        StorageModuleSide.LEFT,
        analysis,
        target,
        candidate,
        xy_reference_detected_cell=xy_reference_detected_cell,
        z_reference_wall_depth_mm=8.0,
    )
    persist_reference_mock.assert_called_once()
    assert persist_reference_mock.call_args.args[1]['xy_reference_detected_cell']['tool_x_mm'] == 11.0
    assert persist_reference_mock.call_args.args[1]['xy_reference_target'] == {'shelf_idx': 0, 'cell_idx': 1}
    assert persist_reference_mock.call_args.args[1]['z_reference_target'] == {'shelf_idx': 0, 'cell_idx': 1}

@pytest.mark.asyncio
async def test_run_io_box_calibration_passes_configured_laser_offset_and_persists_axis_values() -> None:
    manager = LaserAutoCalibrationManager()
    manager._update_snapshot = AsyncMock()
    params = Parameters.model_validate(
        {
            'IDLE_POSITION': [0, 0],
            'PICKUP_POSITION_L': [0, 0],
            'PICKUP_POSITION_H': [0, 0],
            'SCAN_POSITION': [0, 0],
            'STAGE_EXTENSION_IO_BOX': 0,
            'STAGE_NUDGE': 0,
            'STAGE_EXTENSION_SHELF_FRONT': 43,
            'STAGE_EXTENSION_SHELF_BACK': 0,
            'Z_EXTENSION_PICK': 0,
            'Z_EXTENSION_PLACE': 0,
            'Z_EXTENSION_IO': 0,
            'Z_MOVE_RPM': 1,
            'Z_MOVE_ACCEL': 1,
            'ROTATION_FRONT': 0,
            'ROTATION_BACK': 180,
        }
    )
    scan_config = SimpleNamespace(
        laser_sample_settle_ms=50,
        laser_samples_per_point=3,
        laser_offset_x_mm=51.0,
        laser_offset_y_mm=0.0,
    )
    config = SimpleNamespace(
        robot=SimpleNamespace(
            tool=SimpleNamespace(parameters=params),
            grid=SimpleNamespace(scan=scan_config),
        )
    )
    config.model_copy = lambda deep=True: copy.deepcopy(config)
    scout_result = SimpleNamespace(
        calibration_low_x_mm=37.5,
        calibration_low_y_mm=476.0,
        calibration_high_x_mm=37.5,
        calibration_high_y_mm=715.0,
        z_extension_mm=163.5,
        stage_extension_mm=48.0,
    )
    saved_configs: list[object] = []
    worker = SimpleNamespace(_robot=SimpleNamespace(reload_parameters=MagicMock()))

    with patch('openshelf_os_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(laser_module, 'run_io_box_scout', AsyncMock(return_value=scout_result)) as scout_mock:
            with patch('openshelf_os_server.core.laser_auto_calibration.save_root_config', side_effect=lambda next_config: saved_configs.append(next_config)):
                with patch('openshelf_os_server.core.laser_auto_calibration.clear_config_cache'):
                    await manager._run_io_box_calibration(worker)

    assert scout_mock.await_args.kwargs['laser_offset_x_mm'] == pytest.approx(51.0)
    assert scout_mock.await_args.kwargs['laser_offset_y_mm'] == pytest.approx(0.0)
    assert saved_configs[0].robot.tool.parameters.PICKUP_POSITION_L == (37, 476)
    assert saved_configs[0].robot.tool.parameters.PICKUP_POSITION_H == (37, 715)
    assert saved_configs[0].robot.tool.parameters.Z_EXTENSION_IO == pytest.approx(163.5)
    assert saved_configs[0].robot.tool.parameters.STAGE_EXTENSION_IO_BOX == pytest.approx(48.0)
    worker._robot.reload_parameters.assert_called_once_with()


def test_resolve_io_box_laser_offset_rejects_default_zero() -> None:
    with pytest.raises(ValueError, match='laser_offset_x_mm must be configured'):
        _resolve_io_box_laser_offset_x_mm(SimpleNamespace(laser_offset_x_mm=0.0))

    assert _resolve_io_box_laser_offset_x_mm(SimpleNamespace(laser_offset_x_mm=51.0)) == pytest.approx(51.0)


@pytest.mark.asyncio
async def test_run_stage_extension_calibration_accepts_no_progress_terminal_state() -> None:
    manager = LaserAutoCalibrationManager()
    manager._update_snapshot = AsyncMock()
    stage_motor = SimpleNamespace(
        _params=SimpleNamespace(lim=75.0, rpm=300),
        move_to_position=AsyncMock(),
    )
    tool = SimpleNamespace(
        _mx=object(),
        _my=object(),
        _mg=object(),
        _mr=object(),
        _ms=stage_motor,
        retract=AsyncMock(),
        get_param=AsyncMock(side_effect=[187.655, 100.0, 200.0, 200.0]),
    )
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    config = SimpleNamespace(
        robot=SimpleNamespace(
            grid=SimpleNamespace(
                scan=SimpleNamespace(
                    laser_sample_settle_ms=0,
                    laser_scan_z_offset_from_place_mm=None,
                )
            )
        )
    )

    with patch('openshelf_os_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'prepare_capture_pose', AsyncMock()) as prepare_mock:
            with patch.object(
                angle_module,
                'probe_stage_until_stall',
                AsyncMock(return_value=(True, 45.28, 'no_progress', None)),
            ) as probe_mock:
                calibrated_stage_extension_mm = await manager._run_stage_extension_calibration_for_side(
                    worker,
                    StorageModuleSide.RIGHT,
                    ScanBounds(x_min=5.0, x_max=950.0, y_min=0.0, y_max=1250.0),
                    probe_y_mm=1123.0,
                )

    assert calibrated_stage_extension_mm == pytest.approx(40.0)
    assert prepare_mock.await_args.kwargs['should_move_z'] is False
    assert probe_mock.await_args.kwargs['retract_stage_mm'] == pytest.approx(0.0)
    assert probe_mock.await_args.kwargs['fast_approach_mm'] == pytest.approx(0.0)
    assert probe_mock.await_args.kwargs['timeout_s'] == pytest.approx(60.0)
    stage_motor.move_to_position.assert_awaited_once_with(pytest.approx(40.0), rpm=100)
    assert manager._update_snapshot.await_args_list[-1].kwargs['stage_detail']['stall_status'] == 'no_progress'
    assert manager._update_snapshot.await_args_list[-1].kwargs['stage_detail']['calibrated_stage_extension_mm'] == pytest.approx(40.0)
    assert manager._update_snapshot.await_args_list[-1].kwargs['stage_detail']['stage_backoff_mm'] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_run_stage_extension_calibration_rejects_timeout_terminal_state() -> None:
    manager = LaserAutoCalibrationManager()
    manager._update_snapshot = AsyncMock()
    stage_motor = SimpleNamespace(
        _params=SimpleNamespace(lim=75.0, rpm=300),
        move_to_position=AsyncMock(),
    )
    tool = SimpleNamespace(
        _mx=object(),
        _my=object(),
        _mg=object(),
        _mr=object(),
        _ms=stage_motor,
        retract=AsyncMock(),
        get_param=AsyncMock(side_effect=[187.655, 100.0, 200.0, 200.0]),
    )
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    config = SimpleNamespace(
        robot=SimpleNamespace(
            grid=SimpleNamespace(
                scan=SimpleNamespace(
                    laser_sample_settle_ms=0,
                    laser_scan_z_offset_from_place_mm=None,
                )
            )
        )
    )

    with patch('openshelf_os_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'prepare_capture_pose', AsyncMock()):
            with patch.object(
                angle_module,
                'probe_stage_until_stall',
                AsyncMock(return_value=(True, 43.775, 'timeout', None)),
            ):
                with pytest.raises(ValueError, match='timed out before stall'):
                    await manager._run_stage_extension_calibration_for_side(
                        worker,
                        StorageModuleSide.RIGHT,
                        ScanBounds(x_min=5.0, x_max=950.0, y_min=0.0, y_max=1250.0),
                        probe_y_mm=1123.0,
                    )

    assert stage_motor.move_to_position.await_count == 0


@pytest.mark.asyncio
async def test_run_stage_extension_calibration_skips_final_retract_when_cancelled() -> None:
    manager = LaserAutoCalibrationManager()
    manager._update_snapshot = AsyncMock()
    started = asyncio.Event()
    stage_motor = SimpleNamespace(
        _params=SimpleNamespace(lim=75.0, rpm=300),
        move_to_position=AsyncMock(),
    )
    tool = SimpleNamespace(
        _mx=object(),
        _my=object(),
        _mg=object(),
        _mr=object(),
        _ms=stage_motor,
        retract=AsyncMock(),
        get_param=AsyncMock(side_effect=[187.655, 100.0, 200.0, 200.0]),
    )
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    config = SimpleNamespace(
        robot=SimpleNamespace(
            grid=SimpleNamespace(
                scan=SimpleNamespace(
                    laser_sample_settle_ms=0,
                    laser_scan_z_offset_from_place_mm=None,
                )
            )
        )
    )

    async def did_probe_stage_until_stall(*args, **kwargs):
        started.set()
        await asyncio.sleep(10)
        return True, 45.28, 'timeout', None

    with patch('openshelf_os_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'prepare_capture_pose', AsyncMock()):
            with patch.object(angle_module, 'probe_stage_until_stall', side_effect=did_probe_stage_until_stall):
                task = asyncio.create_task(
                    manager._run_stage_extension_calibration_for_side(
                        worker,
                        StorageModuleSide.RIGHT,
                        ScanBounds(x_min=5.0, x_max=950.0, y_min=0.0, y_max=1250.0),
                        probe_y_mm=1123.0,
                    )
                )

                await started.wait()
                task.cancel()

                with pytest.raises(asyncio.CancelledError):
                    await task

    assert tool.retract.await_count == 1


@pytest.mark.asyncio
async def test_run_stage_extension_calibration_raises_when_abort_requested_during_probe() -> None:
    manager = LaserAutoCalibrationManager()
    manager._update_snapshot = AsyncMock()
    stage_motor = SimpleNamespace(
        _params=SimpleNamespace(lim=75.0, rpm=300),
        move_to_position=AsyncMock(),
    )
    tool = SimpleNamespace(
        _mx=object(),
        _my=object(),
        _mg=object(),
        _mr=object(),
        _ms=stage_motor,
        retract=AsyncMock(),
        get_param=AsyncMock(side_effect=[187.655, 100.0, 200.0, 200.0]),
    )
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    config = SimpleNamespace(
        robot=SimpleNamespace(
            grid=SimpleNamespace(
                scan=SimpleNamespace(
                    laser_sample_settle_ms=0,
                    laser_scan_z_offset_from_place_mm=None,
                )
            )
        )
    )

    async def did_probe_stage_until_stall(*args, **kwargs):
        manager._abort_requested = True
        return True, 45.28, 'timeout', None

    with patch('openshelf_os_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'prepare_capture_pose', AsyncMock()):
            with patch.object(angle_module, 'probe_stage_until_stall', side_effect=did_probe_stage_until_stall):
                with pytest.raises(asyncio.CancelledError):
                    await manager._run_stage_extension_calibration_for_side(
                        worker,
                        StorageModuleSide.RIGHT,
                        ScanBounds(x_min=5.0, x_max=950.0, y_min=0.0, y_max=1250.0),
                        probe_y_mm=1123.0,
                    )


@pytest.mark.asyncio
async def test_resolve_stage_extension_probe_y_uses_right_scout_and_offset() -> None:
    manager = LaserAutoCalibrationManager()
    manager._update_snapshot = AsyncMock()
    tool = SimpleNamespace(
        _mx=object(),
        _my=object(),
        _mg=object(),
        _mr=object(),
        _ms=object(),
        retract=AsyncMock(),
        get_param=AsyncMock(side_effect=[100.0, 200.0, 200.0, 187.655]),
    )
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    scan_config = SimpleNamespace(
        laser_sample_settle_ms=0,
        laser_samples_per_point=1,
        laser_scan_stage_extension_mm=None,
        laser_scan_z_offset_from_place_mm=None,
        angle_calibration_x_min_mm=50.0,
        angle_calibration_x_max_mm=940.0,
        angle_calibration_x_step_mm=100.0,
        angle_calibration_x_count=5,
    )
    config = SimpleNamespace(robot=SimpleNamespace(grid=SimpleNamespace(scan=scan_config)))

    with patch('openshelf_os_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'resolve_capture_y_mm', AsyncMock(return_value=1123.0)) as resolve_mock:
            probe_y_mm = await manager._resolve_stage_extension_probe_y_mm(
                worker,
                StorageModuleSide.RIGHT,
                ScanBounds(x_min=5.0, x_max=950.0, y_min=0.0, y_max=1250.0),
            )

    assert probe_y_mm == pytest.approx(1148.0)
    assert resolve_mock.await_args.kwargs['rotation_center_deg'] == pytest.approx(187.655)


def test_build_stage_extension_probe_y_offsets_top_shelf() -> None:
    bounds = ScanBounds(x_min=5.0, x_max=950.0, y_min=0.0, y_max=1250.0)

    assert _build_stage_extension_probe_y_mm(1123.0, bounds) == pytest.approx(1148.0)


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
async def test_resolve_capture_y_mm_raises_cancelled_when_abort_requested_between_scouts() -> None:
    tool = SimpleNamespace(retract=AsyncMock())
    args = SimpleNamespace(y_mm=None, y_range=(0.0, 100.0), scout_x_mm=None)
    scan_config = SimpleNamespace()
    should_abort = False

    async def did_scan_shelf_scout_line(*args, **kwargs):
        nonlocal should_abort
        should_abort = True
        return [], []

    with patch.object(angle_module, 'build_shelf_scout_x_positions', return_value=[10.0, 20.0]):
        with patch.object(angle_module, 'build_axis_positions', return_value=[100.0, 98.0, 96.0]):
            with patch.object(angle_module, 'set_y_brake_for_shelf_scouting', AsyncMock()):
                with patch.object(angle_module, 'enter_capture_mode', AsyncMock()):
                    with patch.object(angle_module, 'prepare_capture_pose', AsyncMock()) as prepare_mock:
                        with patch.object(
                            angle_module,
                            'scan_shelf_scout_line',
                            side_effect=did_scan_shelf_scout_line,
                        ) as scan_mock:
                            with pytest.raises(asyncio.CancelledError):
                                await angle_module.resolve_capture_y_mm(
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
                                    should_abort=lambda: should_abort,
                                )

    assert scan_mock.await_count == 1
    prepare_mock.assert_not_awaited()


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
async def test_read_laser_returns_distance(client) -> None:
    manager = FakeLaserManager(snapshot=_build_snapshot("idle"), is_active=False)
    worker = SimpleNamespace(
        _robot=SimpleNamespace(
            get_laser_distance=AsyncMock(return_value=12.34),
        ),
    )

    with patch(
        "openshelf_os_server.routers.calibration.get_laser_auto_calibration_manager",
        return_value=manager,
    ):
        with patch(
            "openshelf_os_server.routers.calibration.get_robot_worker",
            AsyncMock(return_value=worker),
        ):
            response = await client.get("/api/calibration/laser/read")

    assert response.status_code == 200
    assert response.json()["distance_mm"] == pytest.approx(12.34)
    worker._robot.get_laser_distance.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_laser_auto_calibration_status(client) -> None:
    manager = FakeLaserManager(snapshot=_build_snapshot("idle"), is_active=False)

    with patch(
        "openshelf_os_server.routers.calibration.get_laser_auto_calibration_manager",
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
        "openshelf_os_server.routers.calibration.get_laser_auto_calibration_manager",
        return_value=manager,
    ):
        with patch(
            "openshelf_os_server.routers.calibration.get_robot_worker",
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
    assert manager.last_requested_scope is not None
    assert manager.last_requested_scope.mode == "all"


@pytest.mark.asyncio
async def test_stream_laser_auto_calibration_reuses_running_session() -> None:
    snapshot = _build_snapshot()
    manager = FakeLaserManager(snapshot=snapshot, is_active=True)

    with patch(
        "openshelf_os_server.routers.calibration.get_laser_auto_calibration_manager",
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
async def test_stream_laser_auto_calibration_subscribe_only_does_not_start_when_idle() -> None:
    snapshot = _build_snapshot(state="completed")
    manager = FakeLaserManager(snapshot=snapshot, is_active=False)

    with patch(
        "openshelf_os_server.routers.calibration.get_laser_auto_calibration_manager",
        return_value=manager,
    ):
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        response = await calibration_router.stream_laser_auto_calibration(
            request,
            subscribe_only=True,
        )
        chunk = await anext(response.body_iterator)
        await response.body_iterator.aclose()

    payload = json.loads(chunk["data"])
    assert payload["state"] == "completed"
    assert manager.ensure_calls == 0


@pytest.mark.asyncio
async def test_stream_laser_auto_calibration_closes_after_terminal_initial_snapshot() -> None:
    snapshot = _build_snapshot(state="completed")
    manager = FakeLaserManager(snapshot=snapshot, is_active=False)

    with patch(
        "openshelf_os_server.routers.calibration.get_laser_auto_calibration_manager",
        return_value=manager,
    ):
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        response = await calibration_router.stream_laser_auto_calibration(
            request,
            subscribe_only=True,
        )
        chunk = await anext(response.body_iterator)

        with pytest.raises(StopAsyncIteration):
            await anext(response.body_iterator)

    payload = json.loads(chunk["data"])
    assert payload["state"] == "completed"
    assert manager.unsubscribe_calls == 1

@pytest.mark.asyncio
async def test_stream_laser_auto_calibration_starts_single_shelf_scope() -> None:
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
        "openshelf_os_server.routers.calibration.get_laser_auto_calibration_manager",
        return_value=manager,
    ):
        with patch(
            "openshelf_os_server.routers.calibration.get_robot_worker",
            AsyncMock(return_value=worker),
        ):
            request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
            response = await calibration_router.stream_laser_auto_calibration(
                request,
                side="right_module",
                shelf_idx=2,
                cell_idx=None,
            )
            chunk = await anext(response.body_iterator)
            await response.body_iterator.aclose()

    payload = json.loads(chunk["data"])
    assert payload["requested_scope"]["mode"] == "shelf"
    assert payload["requested_scope"]["side"] == "right_module"
    assert payload["requested_scope"]["shelf_idx"] == 2
    assert manager.last_requested_scope is not None
    assert manager.last_requested_scope.mode == "shelf"
    assert manager.last_requested_scope.side == "right_module"
    assert manager.last_requested_scope.shelf_idx == 2


@pytest.mark.asyncio
async def test_stream_laser_auto_calibration_rejects_mismatched_scope_while_running() -> None:
    snapshot = _build_snapshot()
    snapshot.requested_scope = LaserAutoCalibrationScope(mode="side", side="left_module")
    manager = FakeLaserManager(snapshot=snapshot, is_active=True)

    with patch(
        "openshelf_os_server.routers.calibration.get_laser_auto_calibration_manager",
        return_value=manager,
    ):
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

        with pytest.raises(HTTPException) as exc_info:
            await calibration_router.stream_laser_auto_calibration(
                request,
                side="right_module",
                shelf_idx=None,
                cell_idx=None,
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Laser auto calibration is already running with a different scope"


@pytest.mark.asyncio
async def test_stream_laser_auto_calibration_rejects_cell_without_shelf(client) -> None:
    response = await client.get("/api/calibration/laser/stream?side=left_module&cell_idx=1")

    assert response.status_code == 400
    assert response.json()["detail"] == "shelf_idx is required when cell_idx is provided"


@pytest.mark.asyncio
async def test_abort_laser_auto_calibration(client) -> None:
    manager = FakeLaserManager(snapshot=_build_snapshot("cancelled"), is_active=True)

    with patch(
        "openshelf_os_server.routers.calibration.get_laser_auto_calibration_manager",
        return_value=manager,
    ):
        response = await client.post("/api/calibration/laser/abort")

    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"


@pytest.mark.asyncio
async def test_move_is_blocked_while_laser_auto_calibration_is_running(client) -> None:
    manager = FakeLaserManager(snapshot=_build_snapshot(), is_active=True)

    with patch(
        "openshelf_os_server.routers.calibration.get_laser_auto_calibration_manager",
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
        "openshelf_os_server.routers.demo.is_laser_auto_calibration_running",
        AsyncMock(return_value=True),
    ):
        response = await client.post("/api/demo/cycle/start")

    assert response.status_code == 409
    assert response.json()["detail"] == "laser auto calibration is running"


@pytest.mark.asyncio
async def test_robot_commands_are_blocked_while_laser_auto_calibration_is_running() -> None:
    with patch(
        "openshelf_os_server.routers.robot.is_laser_auto_calibration_running",
        AsyncMock(return_value=True),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await robot_router.robot_commands(
                command=object(),
                worker=SimpleNamespace(),
                current_user=SimpleNamespace(),
                db=SimpleNamespace(),
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "laser auto calibration is running"
