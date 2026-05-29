import json
from unittest.mock import MagicMock, patch

import pytest

from openshelf_os.storage import StorageGrid, StorageValidationError
from openshelf_os_server.routers.storage import _get_robot_grid


MINIMAL_CONFIG = {
	"name": "test",
	"version": "0.1.0",
	"robot": {
		"tool": {
			"parameters": {
				"IDLE_POSITION": [0, 0],
				"PICKUP_POSITION_L": [0, 0],
				"PICKUP_POSITION_H": [0, 0],
				"SCAN_POSITION": [0, 0],
				"STAGE_EXTENSION_IO_BOX": 0,
				"STAGE_NUDGE": 0,
				"STAGE_EXTENSION_SHELF_FRONT": 0,
				"STAGE_EXTENSION_SHELF_BACK": 0,
				"Z_EXTENSION_PICK": 0,
				"Z_EXTENSION_PLACE": 0,
				"Z_EXTENSION_IO": 0,
				"Z_MOVE_RPM": 1,
				"Z_MOVE_ACCEL": 1,
				"ROTATION_FRONT": 0,
				"ROTATION_BACK": 0,
			},
			"motors": [],
			"poe_camera": {
				"url": "",
			},
		},
		"grid": {"path": "storage.json"},
		"io": {
			"motors": [],
		},
	},
	"auth": {"require_auth": False},
}


def _write_config_and_grid(tmp_path, grid_data=None):
	config_path = tmp_path / "config.json"
	config_path.write_text(json.dumps(MINIMAL_CONFIG))

	if grid_data is not None:
		storage_path = tmp_path / "storage.json"
		storage_path.write_text(json.dumps(grid_data))

	return config_path


class TestGetRobotGrid:

	def test_returns_robot_worker_grid_when_available(self):
		fake_grid = StorageGrid()

		mock_robot = MagicMock()
		mock_robot._grid = fake_grid

		mock_worker = MagicMock()
		mock_worker._robot = mock_robot

		with patch("openshelf_os_server.core.robot.robot_worker", mock_worker):
			result = _get_robot_grid()

		assert result is fake_grid

	def test_fallback_loads_from_config_path(self, tmp_path):
		grid_data = {"left_module": [], "right_module": []}
		config_path = _write_config_and_grid(tmp_path, grid_data)

		with (
			patch("openshelf_os_server.core.robot.robot_worker", None),
			patch("openshelf_os.common.CONFIG_PATH", config_path),
		):
			result = _get_robot_grid()

		assert isinstance(result, StorageGrid)
		assert result.left_module == []
		assert result.right_module == []

	def test_fallback_raises_on_missing_storage_file(self, tmp_path):
		config_path = _write_config_and_grid(tmp_path, grid_data=None)

		with (
			patch("openshelf_os_server.core.robot.robot_worker", None),
			patch("openshelf_os.common.CONFIG_PATH", config_path),
		):
			with pytest.raises(StorageValidationError, match="storage file not found"):
				_get_robot_grid()
