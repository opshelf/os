from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from openshelf_os.common import RootConfig
from openshelf_os.config.models import Motor
from openshelf_os.config.settings import DemoFridgeSettings
import openshelf_os.fridge as fridge_module
from openshelf_os.fridge import DemoFridge, FridgeSafetyError
from openshelf_os_server import settings
from openshelf_os_server.routers import calibration, demo


def _fridge_motor() -> Motor:
    return Motor.model_validate(
        {
            "motor_name": "fridge_door",
            "motor_id": 20,
            "Ip": 36,
            "rpm": 200,
            "dr": 0,
            "mpr": 80,
            "accel": 3000,
            "ret_rpm": 64,
            "lim": 814,
        }
    )


def _config() -> RootConfig:
    config = RootConfig.model_validate(settings._build_minimal_config({}))
    config.demo.fridge = DemoFridgeSettings(motor=_fridge_motor())
    return config


@asynccontextmanager
async def _allow_manual_motion(*_args, **_kwargs):
    yield


def _worker(fridge) -> SimpleNamespace:
    return SimpleNamespace(
        _robot=SimpleNamespace(_fridge=fridge),
        needs_home=lambda: False,
        needs_home_axes=lambda: [],
        needs_home_reason=lambda: None,
    )


def _calibration_api() -> FastAPI:
    app = FastAPI()
    app.include_router(calibration.router)
    app.dependency_overrides[calibration.get_current_user] = lambda: None
    return app


@pytest.mark.anyio
async def test_get_fridge_calibration_defaults_to_installed_travel(monkeypatch) -> None:
    monkeypatch.setattr(calibration, "should_enable_demo_fridge", lambda: True)
    monkeypatch.setattr(calibration, "get_root_config", _config)

    response = await calibration.get_fridge_door_calibration()

    assert response.model_dump() == {
        "left": 2.5,
        "center": 407.0,
        "right": 811.5,
        "lower_limit": 0.0,
        "upper_limit": 814.0,
    }


@pytest.mark.anyio
async def test_move_fridge_calibration_uses_requested_position(monkeypatch) -> None:
    door = AsyncMock()
    door._params = _fridge_motor()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        _config().demo.fridge,
        is_fully_retracted=AsyncMock(return_value=True),
    )
    worker = _worker(fridge)
    monkeypatch.setattr(calibration, "ensure_laser_auto_calibration_not_running", AsyncMock())
    monkeypatch.setattr(calibration, "get_robot_worker_or_503", AsyncMock(return_value=worker))
    monkeypatch.setattr(calibration, "manual_motion_guard", _allow_manual_motion)

    response = await calibration.move_fridge_door_for_calibration(
        calibration.FridgeDoorMoveRequest(position=407)
    )

    assert response == {"success": True, "position": 407.0}
    door.move_to_position.assert_awaited_once_with(407, rpm=200)


@pytest.mark.anyio
async def test_move_fridge_calibration_rejects_position_beyond_travel(monkeypatch) -> None:
    fridge = SimpleNamespace(
        _door=SimpleNamespace(_params=_fridge_motor()),
        move_door_to=AsyncMock(),
    )
    worker = _worker(fridge)
    monkeypatch.setattr(calibration, "ensure_laser_auto_calibration_not_running", AsyncMock())
    monkeypatch.setattr(calibration, "get_robot_worker_or_503", AsyncMock(return_value=worker))

    with pytest.raises(HTTPException) as error:
        await calibration.move_fridge_door_for_calibration(
            calibration.FridgeDoorMoveRequest(position=814.1)
        )

    assert error.value.status_code == 400
    fridge.move_door_to.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("position", ["NaN", "Infinity", "-Infinity"])
async def test_move_fridge_calibration_rejects_non_finite_input_before_motion(
    monkeypatch, position: str
) -> None:
    fridge = SimpleNamespace(
        _door=SimpleNamespace(_params=_fridge_motor()),
        move_door_to=AsyncMock(),
    )
    worker = _worker(fridge)
    worker.set_needs_home = Mock()
    get_worker = AsyncMock(return_value=worker)
    classify_fault = AsyncMock()
    monkeypatch.setattr(calibration, "get_robot_worker_or_503", get_worker)
    monkeypatch.setattr(calibration, "_raise_motor_error", classify_fault)

    async with AsyncClient(
        transport=ASGITransport(app=_calibration_api()),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/calibration/fridge/move", json={"position": position})

    assert response.status_code == 422
    get_worker.assert_not_awaited()
    fridge.move_door_to.assert_not_awaited()
    worker.set_needs_home.assert_not_called()
    classify_fault.assert_not_awaited()


@pytest.mark.anyio
async def test_move_fridge_calibration_reports_safety_rejection_without_motor_fault(
    monkeypatch,
) -> None:
    fridge = SimpleNamespace(
        _door=SimpleNamespace(_params=_fridge_motor()),
        move_door_to=AsyncMock(
            side_effect=FridgeSafetyError(
                "Tool must be fully retracted before moving fridge door"
            )
        ),
    )
    worker = _worker(fridge)
    worker.set_needs_home = Mock()
    monkeypatch.setattr(calibration, "ensure_laser_auto_calibration_not_running", AsyncMock())
    monkeypatch.setattr(calibration, "get_robot_worker_or_503", AsyncMock(return_value=worker))
    monkeypatch.setattr(calibration, "manual_motion_guard", _allow_manual_motion)

    with pytest.raises(HTTPException) as error:
        await calibration.move_fridge_door_for_calibration(
            calibration.FridgeDoorMoveRequest(position=407)
        )

    assert error.value.status_code == 409
    assert error.value.detail == "Tool must be fully retracted before moving fridge door"
    worker.set_needs_home.assert_not_called()


@pytest.mark.anyio
async def test_save_fridge_calibration_persists_and_refreshes_runtime(monkeypatch) -> None:
    config = _config()
    runtime_fridge = SimpleNamespace(update_settings=Mock())
    worker = _worker(runtime_fridge)
    save_config = Mock()
    monkeypatch.setattr(calibration, "ensure_laser_auto_calibration_not_running", AsyncMock())
    monkeypatch.setattr(calibration, "should_enable_demo_fridge", lambda: True)
    monkeypatch.setattr(calibration, "get_root_config", lambda: config)
    monkeypatch.setattr(calibration, "save_root_config", save_config)
    monkeypatch.setattr(calibration, "ensure_no_active_bin_workflow_for_motion", AsyncMock())
    monkeypatch.setattr(calibration, "get_robot_worker", AsyncMock(return_value=worker))

    response = await calibration.save_fridge_door_calibration(
        calibration.FridgeDoorSaveRequest(left=2.5, center=407, right=811.5)
    )

    assert response == {"success": True}
    saved_config = save_config.call_args.args[0]
    assert saved_config.demo.fridge.door_positions_mm.model_dump() == {
        "open_left": 2.5,
        "closed": 407.0,
        "open_right": 811.5,
    }
    runtime_fridge.update_settings.assert_called_once_with(saved_config.demo.fridge)


@pytest.mark.anyio
async def test_save_fridge_calibration_rejects_unordered_positions(monkeypatch) -> None:
    monkeypatch.setattr(calibration, "ensure_laser_auto_calibration_not_running", AsyncMock())
    monkeypatch.setattr(calibration, "should_enable_demo_fridge", lambda: True)
    monkeypatch.setattr(calibration, "get_root_config", _config)

    with pytest.raises(HTTPException) as error:
        await calibration.save_fridge_door_calibration(
            calibration.FridgeDoorSaveRequest(left=407, center=2.5, right=811.5)
        )

    assert error.value.status_code == 400


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("left", "NaN"),
        ("center", "Infinity"),
        ("right", "-Infinity"),
    ],
)
async def test_save_fridge_calibration_rejects_non_finite_input_before_persistence(
    monkeypatch, field: str, value: str
) -> None:
    save_config = Mock()
    get_config = Mock(return_value=_config())
    monkeypatch.setattr(calibration, "get_root_config", get_config)
    monkeypatch.setattr(calibration, "save_root_config", save_config)

    request = {"left": 2.5, "center": 407, "right": 811.5}
    request[field] = value
    async with AsyncClient(
        transport=ASGITransport(app=_calibration_api()),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/calibration/fridge/save", json=request)

    assert response.status_code == 422
    get_config.assert_not_called()
    save_config.assert_not_called()


@pytest.mark.anyio
async def test_demo_fridge_shortcut_endpoint_moves_named_position(monkeypatch) -> None:
    fridge = SimpleNamespace(
        move_door_to_named=AsyncMock(),
    )
    worker = _worker(fridge)
    monkeypatch.setattr(demo, "ensure_laser_auto_calibration_not_running", AsyncMock())
    monkeypatch.setattr(demo, "get_robot_worker_or_503", AsyncMock(return_value=worker))
    monkeypatch.setattr(demo, "manual_motion_guard", _allow_manual_motion)

    response = await demo.move_demo_fridge_door("right")

    assert response.message == "fridge door moved to right"
    fridge.move_door_to_named.assert_awaited_once_with("right")


@pytest.mark.anyio
async def test_demo_fridge_center_shortcut_uses_configured_speed(monkeypatch) -> None:
    door = AsyncMock()
    monkeypatch.setattr(fridge_module, "Motor", lambda *_args, **_kwargs: door)
    fridge = DemoFridge(
        AsyncMock(),
        _config().demo.fridge,
        is_fully_retracted=AsyncMock(return_value=True),
    )
    worker = _worker(fridge)
    monkeypatch.setattr(demo, "ensure_laser_auto_calibration_not_running", AsyncMock())
    monkeypatch.setattr(demo, "get_robot_worker_or_503", AsyncMock(return_value=worker))
    monkeypatch.setattr(demo, "manual_motion_guard", _allow_manual_motion)

    response = await demo.move_demo_fridge_door("center")

    assert response.message == "fridge door moved to center"
    door.move_to_position.assert_awaited_once_with(407, rpm=200)
