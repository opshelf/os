import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from perpetual_os1.laser_scan import ScanBounds
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
