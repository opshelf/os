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
					"cell": "F:0:0",
					"data": {
						"name": "Hydrochloric Acid",
						"weight": 590,
					},
				},
				"012345678902": {
					"cell": "B:1:2",
					"nudge_override_mm": 12.5,
				},
			}
		)
	)

	robot = create_robot_loader_stub()
	cell_map = Robot._load_demo_cell_map(robot, str(demo_path))

	assert cell_map is not None
	assert cell_map["012345678901"].side == StorageModuleSide.FRONT
	assert cell_map["012345678901"].shelf_idx == 0
	assert cell_map["012345678901"].cell_idx == 0
	assert cell_map["012345678902"].side == StorageModuleSide.BACK
	assert cell_map["012345678902"].shelf_idx == 1
	assert cell_map["012345678902"].cell_idx == 2
	assert robot._demo_nudge_map[("back_module", 1, 2)] == 12.5


def test_load_demo_cell_map_legacy_schema(tmp_path) -> None:
	demo_path = tmp_path / "demo_legacy.json"
	demo_path.write_text(
		json.dumps(
			{
				"code_cell_mapping": {
					"049000042566": "F:0:0",
					"049000050103": "F:0:1",
				},
				"nudge_override_mm": {
					"F:0:1": 10,
				},
			}
		)
	)

	robot = create_robot_loader_stub()
	cell_map = Robot._load_demo_cell_map(robot, str(demo_path))

	assert cell_map is not None
	assert cell_map["049000042566"].side == StorageModuleSide.FRONT
	assert cell_map["049000042566"].shelf_idx == 0
	assert cell_map["049000042566"].cell_idx == 0
	assert cell_map["049000050103"].side == StorageModuleSide.FRONT
	assert cell_map["049000050103"].shelf_idx == 0
	assert cell_map["049000050103"].cell_idx == 1
	assert robot._demo_nudge_map[("front_module", 0, 1)] == 10.0


def test_load_demo_cell_map_migrates_legacy_side_letters(tmp_path) -> None:
	original = json.dumps(
		{
			"012345678901": "L:0:0",
			"012345678902": {
				"cell": "R:1:2",
				"nudge_override_mm": 12.5,
			},
		}
	)
	demo_path = tmp_path / "demo.json"
	demo_path.write_text(original)

	robot = create_robot_loader_stub()
	cell_map = Robot._load_demo_cell_map(robot, str(demo_path))

	assert cell_map is not None
	assert cell_map["012345678901"].side == StorageModuleSide.FRONT
	assert cell_map["012345678901"].shelf_idx == 0
	assert cell_map["012345678901"].cell_idx == 0
	assert cell_map["012345678902"].side == StorageModuleSide.BACK
	assert cell_map["012345678902"].shelf_idx == 1
	assert cell_map["012345678902"].cell_idx == 2
	assert robot._demo_nudge_map[("back_module", 1, 2)] == 12.5

	migrated = json.loads(demo_path.read_text(encoding="utf-8"))
	assert migrated["012345678901"] == "F:0:0"
	assert migrated["012345678902"]["cell"] == "B:1:2"

	backup_path = demo_path.parent / "demo.json.demo-side-rename.bak"
	assert backup_path.is_file()
	assert backup_path.read_text(encoding="utf-8") == original

	demo_bytes = demo_path.read_bytes()
	backup_bytes = backup_path.read_bytes()
	entries_before = sorted(
		file.relative_to(tmp_path).as_posix() for file in tmp_path.rglob("*") if file.is_file()
	)

	robot = create_robot_loader_stub()
	second_map = Robot._load_demo_cell_map(robot, str(demo_path))

	assert second_map is not None
	assert second_map["012345678901"].side == StorageModuleSide.FRONT
	assert second_map["012345678902"].side == StorageModuleSide.BACK
	assert demo_path.read_bytes() == demo_bytes
	assert backup_path.read_bytes() == backup_bytes
	assert sorted(
		file.relative_to(tmp_path).as_posix() for file in tmp_path.rglob("*") if file.is_file()
	) == entries_before


def test_load_demo_cell_map_migrates_legacy_schema_side_letters(tmp_path) -> None:
	original = json.dumps(
		{
			"code_cell_mapping": {
				"049000042566": "L:0:0",
				"049000050103": "RIGHT_MODULE:0:1",
			},
			"nudge_override_mm": {
				"R:0:1": 10,
			},
		}
	)
	demo_path = tmp_path / "demo.json"
	demo_path.write_text(original)

	robot = create_robot_loader_stub()
	cell_map = Robot._load_demo_cell_map(robot, str(demo_path))

	assert cell_map is not None
	assert cell_map["049000042566"].side == StorageModuleSide.FRONT
	assert cell_map["049000042566"].shelf_idx == 0
	assert cell_map["049000042566"].cell_idx == 0
	assert cell_map["049000050103"].side == StorageModuleSide.BACK
	assert cell_map["049000050103"].shelf_idx == 0
	assert cell_map["049000050103"].cell_idx == 1
	assert robot._demo_nudge_map[("back_module", 0, 1)] == 10.0

	migrated = json.loads(demo_path.read_text(encoding="utf-8"))
	assert migrated["code_cell_mapping"] == {
		"049000042566": "F:0:0",
		"049000050103": "BACK_MODULE:0:1",
	}
	assert migrated["nudge_override_mm"] == {"B:0:1": 10}
	assert (demo_path.parent / "demo.json.demo-side-rename.bak").is_file()


def test_load_demo_cell_map_already_migrated_is_noop(tmp_path) -> None:
	demo_path = tmp_path / "demo.json"
	demo_path.write_text(json.dumps({"012345678901": "F:0:0"}))

	robot = create_robot_loader_stub()
	cell_map = Robot._load_demo_cell_map(robot, str(demo_path))

	assert cell_map is not None
	assert cell_map["012345678901"].side == StorageModuleSide.FRONT
	assert (demo_path.parent / "demo.json.demo-side-rename.bak").is_file() is False


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
