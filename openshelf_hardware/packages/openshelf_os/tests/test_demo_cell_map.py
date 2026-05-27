import json
import logging

import pytest

from openshelf_os.robot import Robot
from openshelf_os.storage import StorageModuleSide


def create_robot_loader_stub() -> Robot:
	robot = Robot.__new__(Robot)
	robot._demo_nudge_map = {}
	robot._logger = logging.getLogger("test_demo_cell_map")
	return robot


def test_load_demo_cell_map_new_schema(tmp_path) -> None:
	demo_path = tmp_path / "demo.json"
	demo_path.write_text(
		json.dumps(
			{
				"012345678901": {
					"cell": "L:0:0",
					"data": {
						"name": "Hydrochloric Acid",
						"weight": 590,
					},
				},
				"012345678902": {
					"cell": "R:1:2",
					"nudge_override_mm": 12.5,
				},
			}
		)
	)

	robot = create_robot_loader_stub()
	cell_map = Robot._load_demo_cell_map(robot, str(demo_path))

	assert cell_map is not None
	assert cell_map["012345678901"].side == StorageModuleSide.LEFT
	assert cell_map["012345678901"].shelf_idx == 0
	assert cell_map["012345678901"].cell_idx == 0
	assert cell_map["012345678902"].side == StorageModuleSide.RIGHT
	assert cell_map["012345678902"].shelf_idx == 1
	assert cell_map["012345678902"].cell_idx == 2
	assert robot._demo_nudge_map[("right_module", 1, 2)] == 12.5


def test_load_demo_cell_map_legacy_schema(tmp_path) -> None:
	demo_path = tmp_path / "demo_legacy.json"
	demo_path.write_text(
		json.dumps(
			{
				"code_cell_mapping": {
					"049000042566": "L:0:0",
					"049000050103": "L:0:1",
				},
				"nudge_override_mm": {
					"L:0:1": 10,
				},
			}
		)
	)

	robot = create_robot_loader_stub()
	cell_map = Robot._load_demo_cell_map(robot, str(demo_path))

	assert cell_map is not None
	assert cell_map["049000042566"].side == StorageModuleSide.LEFT
	assert cell_map["049000042566"].shelf_idx == 0
	assert cell_map["049000042566"].cell_idx == 0
	assert cell_map["049000050103"].side == StorageModuleSide.LEFT
	assert cell_map["049000050103"].shelf_idx == 0
	assert cell_map["049000050103"].cell_idx == 1
	assert robot._demo_nudge_map[("left_module", 0, 1)] == 10.0


def test_load_demo_cell_map_requires_cell_in_new_schema(tmp_path) -> None:
	demo_path = tmp_path / "demo_invalid.json"
	demo_path.write_text(
		json.dumps(
			{
				"012345678901": {
					"nudge_override_mm": 10,
				},
			}
		)
	)

	robot = create_robot_loader_stub()
	with pytest.raises(ValueError, match="must include string field 'cell'"):
		Robot._load_demo_cell_map(robot, str(demo_path))
