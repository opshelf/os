from types import SimpleNamespace
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi import HTTPException
from openshelf_os.config.exceptions import ConfigError
from openshelf_os.operating_mode import OperatingMode
from openshelf_os.robot.commands import RetrieveItemCommand, StoreItemCommand
from openshelf_os_server.errors import CODE_MESSAGES
from openshelf_os_server.routers import robot as robot_router


def _mock_worker():
	worker = MagicMock()
	worker.needs_home = MagicMock(return_value=False)
	worker.needs_home_axes = MagicMock(return_value=[])
	worker.needs_home_reason = MagicMock(return_value="")
	worker.get_scale_data = AsyncMock()
	worker.get_scale_calibration_status = AsyncMock()
	worker.get_scale_calibration_params = AsyncMock()
	worker.set_scale_calibration_params = AsyncMock()
	worker.calibrate_scale_zero = AsyncMock()
	worker.calibrate_scale_weight = AsyncMock()
	worker.scale_tare = AsyncMock()
	worker.enqueue_command = AsyncMock()
	worker.get_cached_scale_status = MagicMock(return_value=None)
	worker.refresh_tray_on_tool = AsyncMock(return_value=False)
	return worker


@pytest.mark.anyio
async def test_scale_endpoint_distinguishes_disabled_scale_from_broken_config() -> None:
	"""The feature-off state and a real config failure must not share a code."""
	worker = _mock_worker()
	worker.get_scale_data.side_effect = ConfigError("Scale is not enabled")

	with patch("openshelf_os_server.routers.robot.get_operating_mode", return_value=OperatingMode.production):
		with pytest.raises(HTTPException) as disabled:
			await robot_router.get_scale_reading(worker=worker)

	assert disabled.value.status_code == 404
	assert disabled.value.detail["code"] == "scale_not_enabled"

	worker.get_scale_data.side_effect = ConfigError("Z_EXT_SCALE not configured")

	with patch("openshelf_os_server.routers.robot.get_operating_mode", return_value=OperatingMode.production):
		with pytest.raises(HTTPException) as broken:
			await robot_router.get_scale_reading(worker=worker)

	assert broken.value.status_code == 503
	assert broken.value.detail == {
		"message": CODE_MESSAGES["scale_config_failed"],
		"code": "scale_config_failed",
	}


@pytest.mark.anyio
async def test_retrieve_command_uses_cached_occupied_scale_status() -> None:
	worker = _mock_worker()
	worker.get_safety_status.return_value = {"bypassed": False}
	worker.get_cached_scale_status.return_value = SimpleNamespace(occupied=True)
	db = AsyncMock()

	with patch("openshelf_os_server.routers.robot.is_laser_auto_calibration_running", AsyncMock(return_value=False)):
		with pytest.raises(HTTPException) as exc_info:
			await robot_router.robot_commands(
				command=RetrieveItemCommand(location={"side": "front_module", "shelf_idx": 0, "cell_idx": 0}),
				worker=worker,
				current_user=SimpleNamespace(id=1),
				db=db,
			)

	assert exc_info.value.status_code == 409
	worker.get_scale_data.assert_not_awaited()


@pytest.mark.anyio
async def test_store_command_rejects_already_stored_item() -> None:
	worker = _mock_worker()
	item = SimpleNamespace(id=42, barcode="123456789012", name="Stored", dimensions={"length": 1.0, "width": 1.0, "height": 1.0}, weight=None)
	location = SimpleNamespace(item_id=42, side="back_module", shelf_idx=1, cell_idx=0)
	item_result = MagicMock()
	item_result.scalar_one_or_none.return_value = item
	location_scalars = MagicMock()
	location_scalars.first.return_value = location
	location_result = MagicMock()
	location_result.scalars.return_value = location_scalars
	no_reserved_bins_result = MagicMock()
	no_reserved_bins_result.scalars.return_value.all.return_value = []
	db = AsyncMock()
	db.execute.side_effect = [no_reserved_bins_result, item_result, location_result]

	with patch("openshelf_os_server.routers.robot.is_laser_auto_calibration_running", AsyncMock(return_value=False)):
		with pytest.raises(HTTPException) as exc_info:
			await robot_router.robot_commands(
				command=StoreItemCommand(
					item={
						"barcode": "123456789012",
						"dimensions": {"length": 1.0, "width": 1.0, "height": 1.0},
					},
				),
				worker=worker,
				current_user=SimpleNamespace(id=1),
				db=db,
			)

	assert exc_info.value.status_code == 409
	worker.enqueue_command.assert_not_awaited()
