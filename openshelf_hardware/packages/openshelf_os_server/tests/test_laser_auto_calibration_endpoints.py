import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from openshelf_os.laser_calibration import angle as angle_module
from openshelf_os.laser_calibration.scan import ScanBounds
from openshelf_os.robot.commands import HomeCommand
from openshelf_os.storage import StorageModuleSide
from openshelf_os_server.core.laser_auto_calibration import LaserAutoCalibrationManager
from openshelf_os_server.routers import robot as robot_router
from openshelf_os_server.core.laser_auto_calibration import LaserAutoCalibrationSnapshot


# Stage-stall tuning moved out of config and into laser_calibration.defaults. Tests that used
# to shape a scan through the scan config pin the constants instead.


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
async def test_home_after_abort_retracts_tool_before_moving_x() -> None:
    manager = LaserAutoCalibrationManager()
    calls = MagicMock()
    tool = SimpleNamespace(
        _mx=SimpleNamespace(move_to_position=AsyncMock(side_effect=lambda *a: calls.x_move())),
        _gs=SimpleNamespace(close=AsyncMock(side_effect=lambda: calls.close())),
        retract_empty=AsyncMock(side_effect=lambda: calls.retract()),
        home=AsyncMock(side_effect=lambda: calls.home()),
    )
    worker = SimpleNamespace(
        _robot=SimpleNamespace(_tool=tool, _io=SimpleNamespace(home_door=AsyncMock())),
        clear_needs_home=MagicMock(),
        get_status_snapshot=AsyncMock(),
    )

    await manager._home_after_abort(worker)

    assert [c[0] for c in calls.mock_calls] == ['close', 'retract', 'x_move', 'home']


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
    assert exc_info.value.detail == "The robot is calibrating."


def test_scan_bounds_are_clamped_to_the_axis_travel() -> None:
    from openshelf_os_server.core.laser_auto_calibration.scan_runner import _clamp_bounds_to_axis_travel

    tool = SimpleNamespace(
        _mx=SimpleNamespace(_params=SimpleNamespace(lim=950.0, lower_lim=0.0)),
        _my=SimpleNamespace(_params=SimpleNamespace(lim=1165.0, lower_lim=0.0)),
    )

    clamped = _clamp_bounds_to_axis_travel(ScanBounds(x_min=5.0, x_max=990.0, y_min=0.0, y_max=1250.0), tool)

    assert clamped == ScanBounds(x_min=5.0, x_max=950.0, y_min=0.0, y_max=1165.0)
    # Without motor limits the configured bounds stand.
    untouched = ScanBounds(x_min=5.0, x_max=950.0, y_min=0.0, y_max=1250.0)
    assert _clamp_bounds_to_axis_travel(untouched, SimpleNamespace()) == untouched
