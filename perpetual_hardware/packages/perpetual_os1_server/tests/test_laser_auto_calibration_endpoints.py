import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from perpetual_os1 import laser_angle_calibration as angle_module
from perpetual_os1.common import Parameters
from perpetual_os1.laser_scan import ScanBounds
from perpetual_os1.storage import StorageModuleSide
from perpetual_os1_server.core.laser_auto_calibration import (
    _build_angle_rotation_center_deg,
    _build_angle_calibration_sample,
    _build_default_scans_dir,
    _build_angle_fit_inputs,
    _build_angle_rotation_positions_for_side,
    _build_angle_x_positions,
    _build_angle_z_offset_positions,
    _build_bounds_for_side,
    _build_stage_extension_probe_y_mm,
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

    rotation_positions = _build_valid_rotation_positions(scan_config, 4.2, 180.0)

    assert rotation_positions[0] == -4.0
    assert rotation_positions[-1] == 8.0
    assert -5.0 not in rotation_positions


def test_build_default_scans_dir_uses_config_data_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "data" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")

    with patch("perpetual_os1_server.core.laser_auto_calibration.get_config_path", return_value=config_path):
        assert _build_default_scans_dir() == str((config_path.parent / "calibration").resolve())


def test_build_valid_rotation_positions_respects_rotation_motor_limit() -> None:
    scan_config = SimpleNamespace(
        angle_calibration_rotation_min_deg=-8.0,
        angle_calibration_rotation_max_deg=8.0,
        angle_calibration_rotation_step_deg=1.0,
    )

    rotation_positions = _build_valid_rotation_positions(scan_config, 358.0, 360.0)

    assert rotation_positions[0] == -8.0
    assert rotation_positions[-1] == 2.0
    assert 3.0 not in rotation_positions


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


def test_build_bounds_for_side_clamps_x_min_away_from_zero() -> None:
    scan_config = SimpleNamespace(
        left_bounds_x_min_mm=0.0,
        left_bounds_x_max_mm=945.0,
        left_bounds_y_min_mm=0.0,
        left_bounds_y_max_mm=1200.0,
        right_bounds_x_min_mm=0.0,
        right_bounds_x_max_mm=945.0,
        right_bounds_y_min_mm=0.0,
        right_bounds_y_max_mm=1200.0,
    )

    left_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.LEFT)
    right_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.RIGHT)

    assert left_bounds.x_min == pytest.approx(5.0)
    assert right_bounds.x_min == pytest.approx(5.0)


def test_build_estimated_rotation_deg_offsets_previous_rotation() -> None:
    fit_result = SimpleNamespace(alpha_deg=4.2)

    estimated_rotation_deg = _build_estimated_rotation_deg(172.8, fit_result)

    assert estimated_rotation_deg == pytest.approx(177.0)


def test_build_angle_fit_inputs_flips_right_side_rotation_sign() -> None:
    raw_samples: list[angle_module.LaserAngleCalibrationSample] = []

    for rotation_deg in [-8.0, 0.0, 8.0]:
        raw_samples.append(
            angle_module.LaserAngleCalibrationSample(
                axis_x_mm=945.0,
                axis_z_mm=140.5,
                rotation_deg=rotation_deg,
                absolute_rotation_deg=180.0 + rotation_deg,
                stage_extension_mm=13.0,
                z_extension_mm=140.5,
                distance_mm=8.0,
            )
        )

    fit_samples, laser_offset_x_mm = _build_angle_fit_inputs(
        StorageModuleSide.RIGHT,
        SimpleNamespace(laser_offset_x_mm=51.0),
        raw_samples,
    )

    assert [sample.rotation_deg for sample in fit_samples] == [8.0, -0.0, -8.0]
    assert [sample.absolute_rotation_deg for sample in fit_samples] == [188.0, 180.0, 172.0]
    assert laser_offset_x_mm == pytest.approx(0.0)


def test_build_angle_rotation_positions_for_right_keeps_non_positive_half() -> None:
    positions = [-8.0, -7.0, -6.0, -5.0, -4.0, -3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]

    filtered_positions = _build_angle_rotation_positions_for_side(StorageModuleSide.RIGHT, positions)

    assert filtered_positions == [-8.0, -7.0, -6.0, -5.0, -4.0, -3.0, -2.0, -1.0, 0.0]


def test_right_side_mirrored_negative_half_sweep_reduces_fit_error() -> None:
    raw_samples: list[angle_module.LaserAngleCalibrationSample] = []
    measured_distances_by_x = {
        945.0: [8.495, 8.361, 8.155, 8.060, 7.908, 7.765, 7.662, 7.547, 7.398, 7.313, 7.187, 7.108, 6.972, 6.905, 6.822, 6.740, 6.679],
        845.0: [8.508, 8.357, 8.207, 8.059, 7.943, 7.808, 7.685, 7.568, 7.432, 7.332, 7.215, 7.107, 7.005, 6.928, 6.852, 6.778, 6.716],
        745.0: [8.513, 8.381, 8.203, 8.047, 7.927, 7.834, 7.681, 7.557, 7.452, 7.348, 7.256, 7.163, 7.062, 6.956, 6.884, 6.795, 6.711],
        645.0: [8.531, 8.443, 8.209, 8.058, 7.964, 7.834, 7.683, 7.565, 7.410, 7.332, 7.253, 7.148, 7.046, 6.973, 6.881, 6.813, 6.764],
        545.0: [8.545, 8.406, 8.250, 8.106, 7.941, 7.807, 7.695, 7.579, 7.484, 7.366, 7.260, 7.142, 7.050, 6.991, 6.891, 6.822, 6.754],
    }
    rotation_positions = [-8.0, -7.0, -6.0, -5.0, -4.0, -3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]

    for axis_x_mm, distances in measured_distances_by_x.items():
        for rotation_deg, distance_mm in zip(rotation_positions, distances):
            raw_samples.append(
                angle_module.LaserAngleCalibrationSample(
                    axis_x_mm=axis_x_mm,
                    axis_z_mm=140.5,
                    rotation_deg=rotation_deg,
                    absolute_rotation_deg=180.0 + rotation_deg,
                    stage_extension_mm=13.0,
                    z_extension_mm=140.5,
                    distance_mm=distance_mm,
                )
            )

    full_fit = angle_module.search_best_alpha(
        raw_samples,
        alpha_min_deg=-20.0,
        alpha_max_deg=20.0,
        coarse_step_deg=0.1,
        fine_window_deg=0.3,
        fine_step_deg=0.005,
        laser_offset_x_mm=0.0,
        laser_offset_z_mm=0.0,
        beam_zero_deg=0.0,
    )
    filtered_positions = set(_build_angle_rotation_positions_for_side(StorageModuleSide.RIGHT, rotation_positions))
    one_sided_samples = [sample for sample in raw_samples if sample.rotation_deg in filtered_positions]
    fit_samples, laser_offset_x_mm = _build_angle_fit_inputs(
        StorageModuleSide.RIGHT,
        SimpleNamespace(laser_offset_x_mm=51.0),
        one_sided_samples,
    )
    one_sided_fit = angle_module.search_best_alpha(
        fit_samples,
        alpha_min_deg=-20.0,
        alpha_max_deg=20.0,
        coarse_step_deg=0.1,
        fine_window_deg=0.3,
        fine_step_deg=0.005,
        laser_offset_x_mm=laser_offset_x_mm,
        laser_offset_z_mm=0.0,
        beam_zero_deg=0.0,
    )

    assert full_fit.alpha_deg == pytest.approx(7.98)
    assert one_sided_fit.alpha_deg == pytest.approx(4.98)
    assert one_sided_fit.line.rms_residual_mm < full_fit.line.rms_residual_mm


def test_left_side_angle_fit_ignores_configured_laser_offset_x() -> None:
    raw_samples: list[angle_module.LaserAngleCalibrationSample] = []
    measured_distances_by_x = {
        945.0: [7.349, 7.230, 7.135, 7.038, 6.949, 6.875, 6.782, 6.723, 6.612],
        845.0: [7.342, 7.240, 7.162, 7.053, 6.958, 6.829, 6.807, 6.705, 6.627],
        745.0: [7.344, 7.251, 7.136, 7.039, 6.955, 6.861, 6.789, 6.722, 6.650],
        645.0: [7.343, 7.271, 7.156, 7.066, 6.953, 6.903, 6.810, 6.714, 6.654],
        545.0: [7.361, 7.249, 7.164, 7.069, 6.952, 6.895, 6.797, 6.748, 6.641],
    }
    rotation_positions = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]

    for axis_x_mm, distances in measured_distances_by_x.items():
        for rotation_deg, distance_mm in zip(rotation_positions, distances):
            raw_samples.append(
                angle_module.LaserAngleCalibrationSample(
                    axis_x_mm=axis_x_mm,
                    axis_z_mm=140.5,
                    rotation_deg=rotation_deg,
                    absolute_rotation_deg=rotation_deg,
                    stage_extension_mm=13.0,
                    z_extension_mm=140.5,
                    distance_mm=distance_mm,
                )
            )

    fit_samples, laser_offset_x_mm = _build_angle_fit_inputs(
        StorageModuleSide.LEFT,
        SimpleNamespace(laser_offset_x_mm=51.0),
        raw_samples,
    )
    fit_result = angle_module.search_best_alpha(
        fit_samples,
        alpha_min_deg=-10.0,
        alpha_max_deg=10.0,
        coarse_step_deg=0.1,
        fine_window_deg=0.3,
        fine_step_deg=0.005,
        laser_offset_x_mm=laser_offset_x_mm,
        laser_offset_z_mm=0.0,
        beam_zero_deg=0.0,
    )

    assert laser_offset_x_mm == pytest.approx(0.0)
    assert fit_result.alpha_deg == pytest.approx(3.82)


def test_build_angle_calibration_sample_keeps_target_z_in_local_offset() -> None:
    sample = _build_angle_calibration_sample(
        x_mm=640.0,
        target_z_mm=140.5,
        rotation_deg=4.0,
        absolute_rotation_deg=184.0,
        stage_extension_mm=13.0,
        depth_mm=55.0,
    )

    assert sample.axis_z_mm == pytest.approx(140.5)
    assert sample.z_extension_mm == pytest.approx(140.5)


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
async def test_run_angle_calibration_skips_final_retract_when_cancelled() -> None:
    manager = LaserAutoCalibrationManager()
    started = asyncio.Event()
    tool = SimpleNamespace(
        _laser_enabled=True,
        _laser=object(),
        _mx=object(),
        _my=object(),
        _mg=object(),
        _ms=object(),
        _mr=SimpleNamespace(_params=SimpleNamespace(lim=360.0)),
        retract=AsyncMock(),
        get_param=AsyncMock(side_effect=[3.815, 100.0, 200.0, 200.0]),
    )
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    scan_config = SimpleNamespace(
        settle_ms=0,
        samples_per_point=1,
        scan_stage_extension_mm=None,
        scan_z_offset_mm=None,
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

    with patch('perpetual_os1_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'resolve_capture_y_mm', side_effect=did_resolve_capture_y_mm):
            task = asyncio.create_task(
                manager._run_angle_calibration_for_side(
                    worker,
                    StorageModuleSide.LEFT,
                    ScanBounds(x_min=5.0, x_max=945.0, y_min=0.0, y_max=1200.0),
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
        _ms=object(),
        _mr=SimpleNamespace(_params=SimpleNamespace(lim=360.0)),
        retract=AsyncMock(),
        get_param=AsyncMock(side_effect=[3.815, 100.0, 200.0, 200.0]),
    )
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    scan_config = SimpleNamespace(
        settle_ms=0,
        samples_per_point=1,
        scan_stage_extension_mm=None,
        scan_z_offset_mm=None,
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

    with patch('perpetual_os1_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'resolve_capture_y_mm', AsyncMock(return_value=1095.0)):
            with patch.object(angle_module, 'prepare_capture_pose', side_effect=did_prepare_capture_pose):
                with patch.object(angle_module, 'sample_laser_depth', AsyncMock(return_value=12.0)):
                    with pytest.raises(asyncio.CancelledError):
                        await manager._run_angle_calibration_for_side(
                            worker,
                            StorageModuleSide.LEFT,
                            ScanBounds(x_min=5.0, x_max=945.0, y_min=0.0, y_max=1200.0),
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

    await callback('phase=shelf-scout scout=1/1 x=945.00')

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

    with patch('perpetual_os1_server.core.laser_auto_calibration._build_bounds_for_side', return_value=ScanBounds(x_min=0.0, x_max=1.0, y_min=0.0, y_max=1.0)):
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
    assert probe_y_mock.await_args_list[1].kwargs['top_shelf_y_mm'] == pytest.approx(left_angle_result.capture_y_mm)

    persisted_results = persist_mock.await_args.args[1]

    assert [result.side for result in persisted_results] == [StorageModuleSide.RIGHT, StorageModuleSide.LEFT]
    assert persisted_results[0].angle_result.updated_rotation_deg == pytest.approx(191.25)


@pytest.mark.asyncio
async def test_run_angle_calibration_uses_geometric_center_not_previous_rotation() -> None:
    manager = LaserAutoCalibrationManager()
    tool = SimpleNamespace(
        _laser_enabled=True,
        _laser=object(),
        _mx=object(),
        _my=object(),
        _mg=object(),
        _ms=object(),
        _mr=SimpleNamespace(_params=SimpleNamespace(lim=360.0)),
        retract=AsyncMock(),
        get_param=AsyncMock(side_effect=[3.815, 100.0, 200.0, 200.0]),
    )
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    scan_config = SimpleNamespace(
        settle_ms=0,
        samples_per_point=1,
        scan_stage_extension_mm=None,
        scan_z_offset_mm=None,
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
        angle_calibration_alpha_min_deg=-10.0,
        angle_calibration_alpha_max_deg=10.0,
        angle_calibration_coarse_step_deg=0.1,
        angle_calibration_fine_window_deg=0.3,
        angle_calibration_fine_step_deg=0.005,
        laser_offset_x_mm=0.0,
        angle_calibration_beam_zero_deg=0.0,
    )
    config = SimpleNamespace(robot=SimpleNamespace(grid=SimpleNamespace(scan=scan_config)))
    fit_result = SimpleNamespace(
        alpha_deg=3.815,
        sample_count=1,
        line=SimpleNamespace(rms_residual_mm=0.1, max_abs_residual_mm=0.2, line_angle_deg=90.0),
    )

    with patch('perpetual_os1_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'resolve_capture_y_mm', AsyncMock(return_value=1095.0)) as resolve_mock:
            with patch.object(angle_module, 'prepare_capture_pose', AsyncMock()):
                with patch.object(angle_module, 'sample_laser_depth', AsyncMock(return_value=5.0)):
                    with patch.object(angle_module, 'search_best_alpha', return_value=fit_result):
                        with patch.object(angle_module, 'print_fit_result'):
                            result = await manager._run_angle_calibration_for_side(
                                worker,
                                StorageModuleSide.LEFT,
                                ScanBounds(x_min=5.0, x_max=945.0, y_min=0.0, y_max=1200.0),
                            )

    assert resolve_mock.await_args.kwargs['rotation_center_deg'] == pytest.approx(0.0)
    assert result.previous_rotation_deg == pytest.approx(3.815)
    assert result.alpha_deg == pytest.approx(3.815)
    assert result.updated_rotation_deg == pytest.approx(3.815)


@pytest.mark.asyncio
async def test_run_angle_calibration_allows_measured_right_rotation_above_180() -> None:
    manager = LaserAutoCalibrationManager()
    tool = SimpleNamespace(
        _laser_enabled=True,
        _laser=object(),
        _mx=object(),
        _my=object(),
        _mg=object(),
        _ms=object(),
        _mr=SimpleNamespace(_params=SimpleNamespace(lim=360.0)),
        retract=AsyncMock(),
        get_param=AsyncMock(side_effect=[183.0, 100.0, 200.0, 200.0]),
    )
    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=tool))
    scan_config = SimpleNamespace(
        settle_ms=0,
        samples_per_point=1,
        scan_stage_extension_mm=None,
        scan_z_offset_mm=None,
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
        angle_calibration_alpha_min_deg=-10.0,
        angle_calibration_alpha_max_deg=10.0,
        angle_calibration_coarse_step_deg=0.1,
        angle_calibration_fine_window_deg=0.3,
        angle_calibration_fine_step_deg=0.005,
        laser_offset_x_mm=0.0,
        angle_calibration_beam_zero_deg=0.0,
    )
    config = SimpleNamespace(robot=SimpleNamespace(grid=SimpleNamespace(scan=scan_config)))
    fit_result = SimpleNamespace(
        alpha_deg=8.0,
        sample_count=1,
        line=SimpleNamespace(rms_residual_mm=0.1, max_abs_residual_mm=0.2, line_angle_deg=90.0),
    )

    with patch('perpetual_os1_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'resolve_capture_y_mm', AsyncMock(return_value=1095.0)) as resolve_mock:
            with patch.object(angle_module, 'prepare_capture_pose', AsyncMock()):
                with patch.object(angle_module, 'sample_laser_depth', AsyncMock(return_value=5.0)):
                    with patch.object(angle_module, 'search_best_alpha', return_value=fit_result):
                        with patch.object(angle_module, 'print_fit_result'):
                            result = await manager._run_angle_calibration_for_side(
                                worker,
                                StorageModuleSide.RIGHT,
                                ScanBounds(x_min=5.0, x_max=945.0, y_min=0.0, y_max=1200.0),
                            )

    assert resolve_mock.await_args.kwargs['rotation_center_deg'] == pytest.approx(180.0)
    assert result.previous_rotation_deg == pytest.approx(183.0)
    assert result.alpha_deg == pytest.approx(8.0)
    assert result.updated_rotation_deg == pytest.approx(188.0)


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
            'JOG_X': 0,
            'JOG_Y': 0,
            'PXMM': 1,
            'PYMM': 1,
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

    with patch('perpetual_os1_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch('perpetual_os1_server.core.laser_auto_calibration.resolve_grid_path', return_value='storage.json'):
            with patch('perpetual_os1_server.core.laser_auto_calibration.laser_module.load_grid', side_effect=[source_grid, reloaded_grid]):
                with patch('perpetual_os1_server.core.laser_auto_calibration.laser_module.build_shelves_from_scan', side_effect=[[object()], [object()]]):
                    with patch('perpetual_os1_server.core.laser_auto_calibration.laser_module.apply_xy_reference_offsets'):
                        with patch('perpetual_os1_server.core.laser_auto_calibration.laser_module.apply_wall_depth_z_offsets'):
                            with patch('perpetual_os1_server.core.laser_auto_calibration.laser_module.StorageGrid', FakeStorageGrid):
                                with patch('perpetual_os1_server.core.laser_auto_calibration.save_root_config', side_effect=lambda next_config: saved_configs.append(next_config)):
                                    with patch('perpetual_os1_server.core.laser_auto_calibration.clear_config_cache'):
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
async def test_run_stage_extension_calibration_accepts_timeout_terminal_state() -> None:
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
                    settle_ms=0,
                    scan_z_offset_mm=None,
                )
            )
        )
    )

    with patch('perpetual_os1_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'prepare_capture_pose', AsyncMock()):
            with patch.object(
                angle_module,
                'probe_stage_until_stall',
                AsyncMock(return_value=(True, 45.28, 'timeout', None)),
            ) as probe_mock:
                calibrated_stage_extension_mm = await manager._run_stage_extension_calibration_for_side(
                    worker,
                    StorageModuleSide.RIGHT,
                    ScanBounds(x_min=5.0, x_max=945.0, y_min=0.0, y_max=1200.0),
                    probe_y_mm=1123.0,
                )

    assert calibrated_stage_extension_mm == pytest.approx(37.28)
    assert probe_mock.await_args.kwargs['retract_stage_mm'] == pytest.approx(0.0)
    assert probe_mock.await_args.kwargs['fast_approach_mm'] == pytest.approx(0.0)
    stage_motor.move_to_position.assert_awaited_once_with(pytest.approx(37.28), rpm=100)
    assert manager._update_snapshot.await_args_list[-1].kwargs['stage_detail']['stall_status'] == 'timeout'
    assert manager._update_snapshot.await_args_list[-1].kwargs['stage_detail']['calibrated_stage_extension_mm'] == pytest.approx(37.28)


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
                    settle_ms=0,
                    scan_z_offset_mm=None,
                )
            )
        )
    )

    async def did_probe_stage_until_stall(*args, **kwargs):
        started.set()
        await asyncio.sleep(10)
        return True, 45.28, 'timeout', None

    with patch('perpetual_os1_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'prepare_capture_pose', AsyncMock()):
            with patch.object(angle_module, 'probe_stage_until_stall', side_effect=did_probe_stage_until_stall):
                task = asyncio.create_task(
                    manager._run_stage_extension_calibration_for_side(
                        worker,
                        StorageModuleSide.RIGHT,
                        ScanBounds(x_min=5.0, x_max=945.0, y_min=0.0, y_max=1200.0),
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
                    settle_ms=0,
                    scan_z_offset_mm=None,
                )
            )
        )
    )

    async def did_probe_stage_until_stall(*args, **kwargs):
        manager._abort_requested = True
        return True, 45.28, 'timeout', None

    with patch('perpetual_os1_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'prepare_capture_pose', AsyncMock()):
            with patch.object(angle_module, 'probe_stage_until_stall', side_effect=did_probe_stage_until_stall):
                with pytest.raises(asyncio.CancelledError):
                    await manager._run_stage_extension_calibration_for_side(
                        worker,
                        StorageModuleSide.RIGHT,
                        ScanBounds(x_min=5.0, x_max=945.0, y_min=0.0, y_max=1200.0),
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
        settle_ms=0,
        samples_per_point=1,
        scan_stage_extension_mm=None,
        scan_z_offset_mm=None,
        angle_calibration_x_min_mm=50.0,
        angle_calibration_x_max_mm=940.0,
        angle_calibration_x_step_mm=100.0,
        angle_calibration_x_count=5,
    )
    config = SimpleNamespace(robot=SimpleNamespace(grid=SimpleNamespace(scan=scan_config)))

    with patch('perpetual_os1_server.core.laser_auto_calibration.get_root_config', return_value=config):
        with patch.object(angle_module, 'resolve_capture_y_mm', AsyncMock(return_value=1123.0)) as resolve_mock:
            probe_y_mm = await manager._resolve_stage_extension_probe_y_mm(
                worker,
                StorageModuleSide.RIGHT,
                ScanBounds(x_min=5.0, x_max=945.0, y_min=0.0, y_max=1200.0),
            )

    assert probe_y_mm == pytest.approx(1148.0)
    assert resolve_mock.await_args.kwargs['rotation_center_deg'] == pytest.approx(187.655)


def test_build_stage_extension_probe_y_offsets_top_shelf() -> None:
    bounds = ScanBounds(x_min=5.0, x_max=945.0, y_min=0.0, y_max=1200.0)

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
                worker=SimpleNamespace(),
                current_user=SimpleNamespace(),
                db=SimpleNamespace(),
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "laser auto calibration is running"
