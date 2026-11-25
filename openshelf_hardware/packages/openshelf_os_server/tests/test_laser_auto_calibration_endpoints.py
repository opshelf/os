import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from openshelf_os.laser_calibration.scan import ScanBounds
from openshelf_os.robot.commands import HomeCommand
from openshelf_os_server.core.laser_auto_calibration import LaserAutoCalibrationManager
from openshelf_os_server.core.laser_auto_calibration.models import LaserAutoCalibrationError
from openshelf_os_server.routers import robot as robot_router
from openshelf_os_server.core.laser_auto_calibration import LaserAutoCalibrationSnapshot


async def _sleep_long(*_args) -> None:
    await asyncio.sleep(10)


@pytest.mark.asyncio
async def test_abort_stops_the_robot_directly_and_homes_only_after_that_stop_returns() -> None:
    """A step slow to unwind must not keep moving, and the direct stop must not land on the home."""
    manager = LaserAutoCalibrationManager()
    calls: list[str] = []
    stop_release = asyncio.Event()

    async def stop() -> None:
        calls.append('stop')
        if len(calls) == 1:
            await stop_release.wait()
            calls.append('stop returned')

    worker = SimpleNamespace(
        _robot=SimpleNamespace(stop_motion=stop),
        set_needs_home=MagicMock(),
        get_status_snapshot=AsyncMock(),
        refresh_tray_on_tool=AsyncMock(return_value=False),
    )

    async def home(_worker) -> None:
        calls.append('home')

    with patch.object(manager, '_validate_requested_scope_config'):
        with patch.object(manager, '_run_requested_scope', AsyncMock(side_effect=_sleep_long)):
            with patch.object(manager, '_home_after_abort', home):
                await manager.ensure_session_started(worker)
                await asyncio.sleep(0)
                abort = asyncio.create_task(manager.abort())
                for _ in range(5):
                    await asyncio.sleep(0)
                assert calls == ['stop']
                stop_release.set()
                await abort
                await manager._task if manager._task is not None else None

    assert calls[:2] == ['stop', 'stop returned']
    assert calls[-1] == 'home'
    assert manager._snapshot.state == 'cancelled'


@pytest.mark.asyncio
async def test_abort_homes_inside_the_motion_lock_even_when_the_step_fails_with_a_hardware_error() -> None:
    """The cancelled step can surface as a motor error with the cancel still pending. The session
    must still stop, home while holding the manual motion lock, and end cancelled."""
    from openshelf_os_server.core import manual_motion

    manager = LaserAutoCalibrationManager()
    manager._abort_requested = True
    calls: list[str] = []
    robot = SimpleNamespace(stop_motion=AsyncMock(side_effect=lambda: calls.append('stop')))
    worker = SimpleNamespace(
        _robot=robot,
        set_needs_home=MagicMock(),
        get_status_snapshot=AsyncMock(),
        refresh_tray_on_tool=AsyncMock(return_value=False),
    )

    async def step(*_args) -> None:
        asyncio.current_task().cancel()
        raise RuntimeError('Motor 2 stopped away from target')

    async def home(_worker) -> None:
        await asyncio.sleep(0)
        calls.append(f'home locked={manual_motion._motion_lock.locked()}')

    with patch.object(manager, '_run_requested_scope', step):
        with patch.object(manager, '_home_after_abort', home):
            await asyncio.create_task(manager._run_session(worker, 'laser-session'))

    assert calls == ['stop', 'home locked=True']
    assert manager._snapshot.state == 'cancelled'
    worker.set_needs_home.assert_not_called()


@pytest.mark.asyncio
async def test_abort_during_failure_bookkeeping_still_ends_in_a_terminal_state() -> None:
    """A step fails for real; an abort lands while the failure handler awaits the diagnostic
    snapshot. The session must end failed with the task cleared, never stuck cancelling."""
    manager = LaserAutoCalibrationManager()
    snapshot_started = asyncio.Event()

    async def status_snapshot(**_kwargs) -> dict:
        snapshot_started.set()
        await asyncio.sleep(10)
        return {}

    worker = SimpleNamespace(
        _robot=SimpleNamespace(stop_motion=AsyncMock()),
        set_needs_home=MagicMock(),
        get_status_snapshot=status_snapshot,
        refresh_tray_on_tool=AsyncMock(return_value=False),
    )

    with patch.object(manager, '_validate_requested_scope_config'):
        with patch.object(manager, '_run_requested_scope', AsyncMock(side_effect=RuntimeError('motor stalled'))):
            await manager.ensure_session_started(worker)
            task = manager._task
            await asyncio.wait_for(snapshot_started.wait(), 5)
            await manager.abort()
            await asyncio.wait_for(task, 5)

    assert manager._snapshot.state == 'failed'
    assert manager._task is None
    with pytest.raises(RuntimeError):
        await manager.abort()


@pytest.mark.asyncio
async def test_run_session_cancel_homes_via_abort_without_needs_home() -> None:
    manager = LaserAutoCalibrationManager()
    worker = SimpleNamespace(
        _robot=SimpleNamespace(stop_motion=AsyncMock()),
        set_needs_home=MagicMock(),
        get_status_snapshot=AsyncMock(),
        refresh_tray_on_tool=AsyncMock(return_value=False),
    )

    with patch.object(manager, '_run_all_sides', AsyncMock(side_effect=asyncio.CancelledError)):
        with patch.object(manager, '_set_terminal_state', AsyncMock()) as terminal_mock:
            with patch.object(manager, '_home_after_abort', AsyncMock()) as home_mock:
                await manager._run_session(worker, 'laser-session')

    worker.set_needs_home.assert_not_called()
    worker.get_status_snapshot.assert_not_awaited()
    terminal_mock.assert_awaited_once_with('cancelled', None)
    home_mock.assert_awaited_once_with(worker)


@pytest.mark.asyncio
async def test_abort_during_grip_probe_with_contact_left_flags_needs_home() -> None:
    """The abort cancels the probe while the fingers are in a tray hole and still touch it after
    the close: the session must end failed with needs_home, never stuck cancelling."""
    from hardware.servo import GripState
    from openshelf_os.common import AxisCoordinate
    from openshelf_os.laser_calibration.grip_edge import probe_grip_at
    from openshelf_os.tool import Tool

    manager = LaserAutoCalibrationManager()
    manager._abort_requested = True
    tool = Tool.__new__(Tool)
    tool.is_grip_detection_enabled = True
    tool._gs = SimpleNamespace(
        open=AsyncMock(side_effect=asyncio.CancelledError),
        close=AsyncMock(return_value=GripState(0x71, 0, 0x90, 0x40)),
    )
    tool.set_motion_diagnostic_context = MagicMock()
    tool.extend_empty = AsyncMock()
    tool.retract_empty = AsyncMock()
    worker = SimpleNamespace(
        _robot=SimpleNamespace(_tool=tool, stop_motion=AsyncMock()),
        set_needs_home=MagicMock(),
        get_status_snapshot=AsyncMock(),
        refresh_tray_on_tool=AsyncMock(return_value=False),
    )

    async def probe(*_args) -> None:
        await probe_grip_at(tool, AxisCoordinate(x=1.0, y=2.0, s=0), 3.0)

    with patch.object(manager, '_run_requested_scope', probe):
        await manager._run_session(worker, 'laser-session')

    worker.set_needs_home.assert_called_once()
    assert manager._snapshot.state == 'failed'
    tool.retract_empty.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_session_keeps_the_exception_out_of_the_client_snapshot(caplog) -> None:
    """Clients get the phase and the correlation id of the log line; the exception text,
    fault context and diagnostic snapshot stay in the log and the run metadata."""
    manager = LaserAutoCalibrationManager()
    manager._snapshot.current_stage = 'scan'
    worker = SimpleNamespace(
        set_needs_home=MagicMock(),
        get_status_snapshot=AsyncMock(return_value={'port': '/dev/ttyUSB0'}),
        refresh_tray_on_tool=AsyncMock(return_value=False),
    )
    failure = RuntimeError('motor 3 timed out on /dev/ttyUSB0')
    failure.fault_context = {'alarm_code': 24}

    with patch.object(manager, '_run_requested_scope', AsyncMock(side_effect=failure)):
        with caplog.at_level('ERROR'):
            await manager._run_session(worker, 'laser-session')

    snapshot = manager._snapshot
    assert snapshot.state == 'failed'
    assert snapshot.error_detail is not None
    assert snapshot.error_detail.model_dump() == {'phase': 'scan', 'correlation_id': None}
    client_view = snapshot.model_dump_json()
    for leak in ('/dev/ttyUSB0', 'motor 3', 'alarm_code', 'RuntimeError'):
        assert leak not in client_view, leak
    record = next(r for r in caplog.records if r.getMessage() == 'laser_auto_calibration_failed')
    assert record.exc_info is not None
    assert record.error_detail['error'] == 'motor 3 timed out on /dev/ttyUSB0'
    assert record.error_detail['fault_context'] == {'alarm_code': 24}


@pytest.mark.asyncio
async def test_home_after_abort_retracts_tool_before_moving_x() -> None:
    manager = LaserAutoCalibrationManager()
    calls = MagicMock()
    tool = SimpleNamespace(
        _mx=SimpleNamespace(move_to_position=AsyncMock(side_effect=lambda *a: calls.x_move())),
        release=AsyncMock(side_effect=lambda _phase: calls.close()),
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
    assert exc_info.value.detail == {"message": "The robot is calibrating.", "code": "calibration_running"}


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


@pytest.mark.asyncio
async def test_persist_failure_metadata_marks_active_scan_run_failed(tmp_path) -> None:
    manager = LaserAutoCalibrationManager()
    metadata_path = tmp_path / 'metadata.json'
    metadata_path.write_text(json.dumps({'run_id': 'scan-run', 'status': 'running'}), encoding='utf-8')
    error_detail = LaserAutoCalibrationError(error_type='RuntimeError', error='boom')

    await manager._persist_failure_metadata(error_detail)
    assert json.loads(metadata_path.read_text(encoding='utf-8'))['status'] == 'running'

    manager._run_dir = tmp_path
    await manager._persist_failure_metadata(error_detail)

    data = json.loads(metadata_path.read_text(encoding='utf-8'))
    assert data['status'] == 'failed'
    assert data['error'] == 'boom'
    assert data['error_type'] == 'RuntimeError'


@pytest.mark.asyncio
async def test_io_box_scout_session_reports_the_measurement_and_never_saves_the_config(tmp_path) -> None:
    """The io box scout is measure-only: a completed run carries its result in the snapshot and
    leaves the config untouched, so a scout can never move a hand-calibrated pose."""
    import contextlib

    from openshelf_os.laser_calibration.io_box_scout import IOBoxManualPose, IOBoxScoutResult
    from openshelf_os_server.core.laser_auto_calibration import LaserAutoCalibrationScope
    from openshelf_os_server.core.laser_auto_calibration import scope_runner

    @contextlib.asynccontextmanager
    async def hold_enabled():
        yield

    def motor(lim: float) -> SimpleNamespace:
        return SimpleNamespace(_params=SimpleNamespace(lim=lim, lower_lim=0.0), hold_enabled=hold_enabled)

    worker = SimpleNamespace(
        _robot=SimpleNamespace(_tool=SimpleNamespace(_mx=motor(950.0), _my=motor(1165.0)), stop_motion=AsyncMock()),
        set_needs_home=MagicMock(),
        get_status_snapshot=AsyncMock(),
        refresh_tray_on_tool=AsyncMock(return_value=False),
    )
    result = IOBoxScoutResult(
        laser_offset_x_mm=51.0,
        laser_offset_y_mm=0.0,
        gripper_correction_x_mm=0.0,
        gripper_correction_y_mm=0.0,
        manual=IOBoxManualPose(None, None, (24.0, 465.0), None, 174.0, None, 52.0, 160.5),
        y_line_x_mm=10.0,
        plate_top_tool_y_mm=430.0,
    )
    scout = AsyncMock(return_value=result)
    manager = LaserAutoCalibrationManager()
    manager._scans_dir = str(tmp_path)

    with (
        patch.object(manager, '_validate_requested_scope_config'),
        patch.object(scope_runner, 'run_io_box_scout', scout),
        patch.object(scope_runner, 'save_root_config') as save,
    ):
        await manager.ensure_session_started(worker, LaserAutoCalibrationScope(mode='io_box'))
        await manager._task

    snapshot = await manager.get_snapshot()
    assert snapshot.state == 'completed', snapshot.error
    assert snapshot.io_box_scout is not None
    assert snapshot.io_box_scout['plate_top_tool_y_mm'] == 430.0
    assert scout.await_args.kwargs['x_travel_min_mm'] == 0.0
    assert scout.await_args.kwargs['run_dir'].parent.name == 'io_box_scout'
    save.assert_not_called()
