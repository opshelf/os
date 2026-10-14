import asyncio
import contextlib
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from openshelf_os.laser_calibration import angle as angle_module
from openshelf_os.common import Parameters
from openshelf_os.laser_calibration.scan import ScanBounds
from openshelf_os.robot.commands import HomeCommand
from openshelf_os.storage import StorageModuleSide
from openshelf_os_server.core.laser_auto_calibration import (
    _SideState,
    LaserAutoCalibrationAngleResult,
    LaserAutoCalibrationScope,
    LaserAutoCalibrationManager,
)
from openshelf_os_server.core.laser_auto_calibration.apply_runner import (
    _preserve_manual_cells,
)
from openshelf_os.storage import (
    CalibrationCoordinate,
    Cell,
    CellStatus,
    Dimensions,
    Shelf,
)
from openshelf_os_server.routers import robot as robot_router
from openshelf_os_server.core.laser_auto_calibration import LaserAutoCalibrationSnapshot


# Stage-stall tuning moved out of config and into laser_calibration.defaults. Tests that used
# to shape a scan through the scan config pin the constants instead.


def _build_reference_bounds_scan_config(**overrides) -> SimpleNamespace:
    values = {
        "laser_offset_x_mm": 50.0,
        "laser_offset_y_mm": 0.0,
        "front_bounds_x_min_mm": 0.0,
        "front_bounds_x_max_mm": 950.0,
        "front_bounds_y_min_mm": 0.0,
        "front_bounds_y_max_mm": 1250.0,
        "back_bounds_x_min_mm": 0.0,
        "back_bounds_x_max_mm": 950.0,
        "back_bounds_y_min_mm": 0.0,
        "back_bounds_y_max_mm": 1250.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_ensure_session_started_rejects_invalid_config_before_starting_background_task() -> None:
    manager = LaserAutoCalibrationManager()
    scan_config = _build_reference_bounds_scan_config()
    config = SimpleNamespace(robot=SimpleNamespace(grid=SimpleNamespace(scan=scan_config)))

    with patch("openshelf_os_server.core.laser_auto_calibration.get_root_config", return_value=config):
        with pytest.raises(ValueError, match="front_gripper_xy_correction_mm must be configured"):
            await manager.ensure_session_started(
                SimpleNamespace(),
                LaserAutoCalibrationScope(mode="side", side=StorageModuleSide.FRONT),
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

    tool._mx.move_to_position.assert_awaited_once_with(angle_module.BACK_MODULE_ROTATION_SAFE_X_MM)
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
async def test_run_session_cancel_homes_via_abort_without_needs_home() -> None:
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
@pytest.mark.parametrize("hold_y_enabled", [True, False])
async def test_run_session_holds_y_enabled_only_when_requested(hold_y_enabled: bool) -> None:
    manager = LaserAutoCalibrationManager()
    held: list[bool] = []

    @contextlib.asynccontextmanager
    async def hold_enabled():
        held.append(True)
        yield

    worker = SimpleNamespace(_robot=SimpleNamespace(_tool=SimpleNamespace(_my=SimpleNamespace(hold_enabled=hold_enabled))))
    scope = LaserAutoCalibrationScope(hold_y_enabled=hold_y_enabled)

    with patch.object(manager, '_get_current_calibration_params', AsyncMock(return_value={})):
        with patch.object(manager, '_run_requested_scope', AsyncMock()) as run_mock:
            with patch.object(manager, '_set_terminal_state', AsyncMock()) as terminal_mock:
                await manager._run_session(worker, 'laser-session', scope)

    run_mock.assert_awaited_once_with(worker, scope)
    terminal_mock.assert_awaited_once_with('completed', None)
    assert held == ([True] if hold_y_enabled else [])


@pytest.mark.asyncio
async def test_scan_status_callback_does_not_overwrite_terminal_state(tmp_path: Path) -> None:
    manager = LaserAutoCalibrationManager()
    await manager._set_terminal_state("completed", None)
    callback = manager._build_scan_status_callback(
        "front_module",
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
        laser_scan_pose_stage_mm=None,
        laser_scan_pose_z_delta_from_place_mm=None,
        angle_calibration_x_max_mm=940.0,
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
                    StorageModuleSide.FRONT,
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
        laser_scan_pose_stage_mm=None,
        laser_scan_pose_z_delta_from_place_mm=None,
        angle_calibration_x_max_mm=940.0,
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
                            StorageModuleSide.FRONT,
                            ScanBounds(x_min=5.0, x_max=950.0, y_min=0.0, y_max=1250.0),
                        )


@pytest.mark.asyncio
async def test_scan_status_callback_preserves_cancelling_snapshot() -> None:
    manager = LaserAutoCalibrationManager()
    manager._abort_requested = True
    manager._snapshot = LaserAutoCalibrationSnapshot(
        state='running',
        session_id='laser-session',
        current_side='front_module',
        current_stage='cancelling',
        updated_at='2026-01-01T00:00:00+00:00',
    )
    manager._refresh_scan_side_state = AsyncMock()
    manager._update_snapshot = AsyncMock()
    callback = manager._build_scan_status_callback(
        'front_module',
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
        side='front_module',
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
        side='back_module',
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
            with patch.object(manager, '_apply_angle_calibration', AsyncMock()) as apply_mock:
                with patch.object(manager, '_resolve_stage_extension_probe_y_mm', AsyncMock(side_effect=[1148.0, 1120.0])) as probe_y_mock:
                    with patch.object(manager, '_run_stage_extension_calibration_for_side', AsyncMock(side_effect=[44.0, 41.0])) as stage_mock:
                        with patch.object(manager, '_apply_stage_extension_calibration', AsyncMock()) as apply_stage_mock:
                            with patch.object(manager, '_run_side', side_effect=did_run_side):
                                with patch.object(manager, '_persist_completed_results', AsyncMock()) as persist_mock:
                                    await manager._run_all_sides(worker)

    assert processed_sides == [StorageModuleSide.BACK, StorageModuleSide.FRONT]
    assert run_angle_mock.await_count == 2
    assert apply_mock.await_count == 2
    assert stage_mock.await_count == 2
    assert apply_stage_mock.await_count == 2
    persist_mock.assert_awaited_once()
    assert probe_y_mock.await_count == 2

    assert run_angle_mock.await_args_list[0].args[1] == StorageModuleSide.BACK
    assert run_angle_mock.await_args_list[1].args[1] == StorageModuleSide.FRONT
    assert stage_mock.await_args_list[0].args[1] == StorageModuleSide.BACK
    assert stage_mock.await_args_list[1].args[1] == StorageModuleSide.FRONT
    assert stage_mock.await_args_list[0].kwargs['probe_y_mm'] == pytest.approx(1148.0)
    assert stage_mock.await_args_list[1].kwargs['probe_y_mm'] == pytest.approx(1120.0)
    assert probe_y_mock.await_args_list[0].args[1] == StorageModuleSide.BACK
    assert probe_y_mock.await_args_list[1].args[1] == StorageModuleSide.FRONT
    assert probe_y_mock.await_args_list[0].kwargs['top_shelf_y_mm'] == pytest.approx(right_angle_result.capture_y_mm)
    assert probe_y_mock.await_args_list[1].kwargs['top_shelf_y_mm'] == pytest.approx(left_angle_result.capture_y_mm)

    persisted_results = persist_mock.await_args.args[1]

    assert [result.side for result in persisted_results] == [StorageModuleSide.BACK, StorageModuleSide.FRONT]
    assert persisted_results[0].angle_result.updated_rotation_deg == pytest.approx(191.25)


@pytest.mark.asyncio
async def test_persist_completed_results_saves_stage_extensions() -> None:
    manager = LaserAutoCalibrationManager()
    manager._side_states['front_module'] = _SideState(stage_extension_mm=41.0)
    manager._side_states['back_module'] = _SideState(stage_extension_mm=44.0)

    params = Parameters.model_validate(
        {
            'IDLE_POSITION': [0, 0],
            'PICKUP_POSITION_L': [0, 0],
            'PICKUP_POSITION_H': [0, 0],
            'PICKUP_POSITION_MULTI_ITEM': [0, 0],
            'SCAN_POSITION': [0, 0],
            'STAGE_EXTENSION_IO_BOX': 0,
            'STAGE_EXTENSION_IO_BOX_MULTI_ITEM': 0,
            'STAGE_NUDGE': 0,
            'STAGE_EXTENSION_SHELF_FRONT': 0,
            'STAGE_EXTENSION_SHELF_BACK': 0,
            'Z_EXTENSION_PICK': 0,
            'Z_EXTENSION_PLACE': 0,
            'Z_EXTENSION_IO': 0,
            'Z_EXTENSION_IO_MULTI_ITEM': 0,
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

    source_grid = SimpleNamespace(front_module=[], back_module=[], filename='storage.json', save=MagicMock())
    reloaded_grid = SimpleNamespace()
    saved_configs: list[tuple[object, str | None]] = []
    saved_storage_grids: list[object] = []

    class FakeStorageGrid:
        def __init__(self, *, front_module, back_module, filename) -> None:
            self.front_module = front_module
            self.back_module = back_module
            self.filename = filename

        def save(self, *, version_id: str | None = None) -> None:
            self.version_id = version_id
            saved_storage_grids.append(self)

    worker = SimpleNamespace(_robot=SimpleNamespace(reload_parameters=MagicMock(), _grid=None))
    analysis = SimpleNamespace(shelves=[object()], metadata_path=Path('/tmp/fake-metadata.json'))
    completed_results = [
        SimpleNamespace(
            side=StorageModuleSide.FRONT,
            angle_result=LaserAutoCalibrationAngleResult(
                side='front_module',
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
            side=StorageModuleSide.BACK,
            angle_result=LaserAutoCalibrationAngleResult(
                side='back_module',
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

    with patch.object(manager, '_resolve_z_delta_model', AsyncMock(return_value=(4.85, -0.00285))):
        with patch('openshelf_os_server.core.laser_auto_calibration.get_root_config', return_value=config):
            with patch('openshelf_os_server.core.laser_auto_calibration.resolve_grid_path', return_value='storage.json'):
                with patch('openshelf_os_server.core.laser_auto_calibration.laser_module.load_grid', side_effect=[source_grid, reloaded_grid]):
                    with patch('openshelf_os_server.core.laser_auto_calibration.laser_module.build_shelves_from_scan', side_effect=[[object()], [object()]]):
                        with patch('openshelf_os_server.core.laser_auto_calibration.laser_module.apply_xy_reference_offsets'):
                            with patch('openshelf_os_server.core.laser_auto_calibration.laser_module.apply_wall_depth_z_offsets'):
                                with patch('openshelf_os_server.core.laser_auto_calibration.laser_module.StorageGrid', FakeStorageGrid):
                                    with patch(
                                        'openshelf_os_server.core.laser_auto_calibration.save_root_config',
                                        side_effect=lambda next_config, *, version_id=None: saved_configs.append((next_config, version_id)),
                                    ):
                                        with patch('openshelf_os_server.core.laser_auto_calibration.clear_config_cache'):
                                            # The stage-extension assertions below are what this test is about;
                                            # the reference-frame derivation needs a real grid and a matched opening.
                                            with patch.object(manager, '_derive_reference_offset', AsyncMock(return_value=None)):
                                                await manager._persist_completed_results(worker, completed_results)

    assert len(saved_storage_grids) == 1
    assert len(saved_configs) == 1
    saved_config, config_version_id = saved_configs[0]
    assert saved_config.robot.tool.parameters.ROTATION_FRONT == pytest.approx(4.0)
    assert saved_config.robot.tool.parameters.ROTATION_BACK == pytest.approx(184.0)
    assert saved_config.robot.tool.parameters.STAGE_EXTENSION_SHELF_FRONT == pytest.approx(41.0)
    assert saved_config.robot.tool.parameters.STAGE_EXTENSION_SHELF_BACK == pytest.approx(44.0)
    assert saved_storage_grids[0].version_id == config_version_id
    assert config_version_id is not None
    worker._robot.reload_parameters.assert_called_once_with()
    assert worker._robot._grid is reloaded_grid


def _manual_cell(cell_idx: int, x: float, y: float, is_manual: bool = False) -> Cell:
    return Cell(
        cell_idx=cell_idx,
        status=CellStatus.AVAILABLE,
        item=None,
        calibration_point=CalibrationCoordinate(x=x, y=y, z_offset=10.0, s=0),
        dimensions=Dimensions(width=40.0, height=50.0, length=60.0),
        is_manual=is_manual,
    )


def _manual_shelf(shelf_idx: int, cells: list[Cell]) -> Shelf:
    return Shelf(shelf_idx=shelf_idx, cells=cells)


def test_preserve_manual_cells_reinserts_manual_and_renumbers() -> None:
    scanned = [_manual_shelf(0, [_manual_cell(0, 100.0, 300.0), _manual_cell(1, 200.0, 300.0), _manual_cell(2, 300.0, 300.0)])]
    existing = [_manual_shelf(0, [_manual_cell(0, 100.0, 300.0), _manual_cell(9, 400.0, 302.0, is_manual=True)])]

    result = _preserve_manual_cells(scanned, existing)

    cells = sorted(result[0].cells, key=lambda cell: cell.cell_idx)
    assert [(c.cell_idx, round(c.calibration_point.x, 1), c.is_manual) for c in cells] == [
        (0, 100.0, False),
        (1, 200.0, False),
        (2, 300.0, False),
        (3, 400.0, True),
    ]


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
                    laser_scan_pose_z_delta_from_place_mm=None,
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
                        StorageModuleSide.BACK,
                        ScanBounds(x_min=5.0, x_max=950.0, y_min=0.0, y_max=1250.0),
                        probe_y_mm=1123.0,
                    )


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
                                    side_name='front',
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
async def test_robot_commands_are_blocked_while_laser_auto_calibration_is_running() -> None:
    with patch(
        "openshelf_os_server.routers.robot.is_laser_auto_calibration_running",
        AsyncMock(return_value=True),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await robot_router.robot_commands(
                command=HomeCommand(),
                worker=SimpleNamespace(),
                current_user=SimpleNamespace(),
                db=SimpleNamespace(),
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "laser auto calibration is running"


def test_restore_calibration_params_keeps_persisted_values() -> None:
    manager = LaserAutoCalibrationManager()
    reload_mock = MagicMock()
    worker = SimpleNamespace(_robot=SimpleNamespace(reload_parameters=reload_mock))

    # A run that failed after the angle stage must not roll rotation back to the pre-run value:
    # the calibration is already committed to disk, so reloading from disk is the correct restore.
    with patch('openshelf_os_server.core.laser_auto_calibration.apply_runner.clear_config_cache'):
        manager._restore_in_memory_calibration_params(
            worker,
            {
                'ROTATION_FRONT': 182.0,
                'ROTATION_BACK': 2.0,
                'STAGE_EXTENSION_SHELF_FRONT': 46.0,
                'STAGE_EXTENSION_SHELF_BACK': 47.0,
            },
        )

    reload_mock.assert_called_once()
