import inspect
import json
import os

import pytest

from openshelf_os.common import RootConfig, resolve_grid_path, CONFIG_PATH, write_json_file_with_backup
from openshelf_os.storage import StorageGrid, StorageValidationError


MINIMAL_CONFIG_DICT = {
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
				"JOG_X": 0,
				"JOG_Y": 0,
				"PXMM": 1,
				"PYMM": 1,
			},
			"motors": [],
			"poe_camera": {
				"ip": "127.0.0.1",
				"port": 80,
				"url": "",
				"H_ref_size": [1920, 1080],
				"pattern_size": [9, 6],
				"square_size_mm": 25.0,
			},
		},
		"grid": {"path": "REPLACE_ME"},
		"io": {
			"motors": [],
			"usb_camera": {
				"usb_index": 0,
				"usb_fourcc": "MJPG",
				"usb_exposure": 25,
				"square_mm": 7.5,
				"fx_px": 708,
				"fy_px": 708,
				"px_per_mm": 1.2,
				"confidence_min": 0,
				"headless_fallback_path": "./fallback.jpg",
			},
		},
	},
	"auth": {"require_auth": False},
}


def _make_config(grid_path: str) -> RootConfig:
	data = json.loads(json.dumps(MINIMAL_CONFIG_DICT))
	data["robot"]["grid"]["path"] = grid_path
	return RootConfig.model_validate(data)


EMPTY_GRID = {"left_module": [], "right_module": []}

GRID_WITH_SHELVES = {
	"left_module": [
		{
			"shelf_height": 200.0,
			"shelf_width": 400.0,
			"cells": [
				{
					"status": "available",
					"item": None,
					"calibration_point": {"x": 10.0, "y": 20.0, "s": 0, "z_offset": 0.0},
					"dimensions": {"length": 100.0, "width": 50.0, "height": 150.0},
				}
			],
		}
	],
	"right_module": [
		{
			"shelf_height": 200.0,
			"shelf_width": 400.0,
			"cells": [
				{
					"status": "occupied",
					"item": {
						"dimensions": {"length": 80.0, "width": 40.0, "height": 100.0},
						"weight": 1.5,
					},
					"calibration_point": {"x": 30.0, "y": 40.0, "s": 1, "z_offset": 0.0},
					"dimensions": {"length": 100.0, "width": 50.0, "height": 150.0},
				},
				{
					"status": "available",
					"item": None,
					"calibration_point": {"x": 90.0, "y": 40.0, "s": 1, "z_offset": 0.0},
					"dimensions": {"length": 100.0, "width": 100.0, "height": 150.0},
				},
			],
		}
	],
}


class TestResolveGridPath:

	def test_relative_path(self):
		config = _make_config("storage.json")
		result = resolve_grid_path(config)

		expected = str((CONFIG_PATH.parent / "storage.json").resolve())

		assert result == expected

	def test_dotslash_resolves_same(self):
		config_plain = _make_config("storage.json")
		config_dot = _make_config("./storage.json")

		assert resolve_grid_path(config_plain) == resolve_grid_path(config_dot)

	def test_nested_subdirectory(self):
		config = _make_config("grids/main.json")
		result = resolve_grid_path(config)

		expected = str((CONFIG_PATH.parent / "grids/main.json").resolve())

		assert result == expected

	def test_result_is_absolute(self):
		config = _make_config("storage.json")
		result = resolve_grid_path(config)

		assert os.path.isabs(result) is True


class TestLoadFromFile:

	def test_file_not_found(self, tmp_path):
		missing = str(tmp_path / "nope.json")

		with pytest.raises(StorageValidationError, match="storage file not found"):
			StorageGrid.load_from_file(missing)

	def test_invalid_json(self, tmp_path):
		bad_file = tmp_path / "bad.json"
		bad_file.write_text("{not valid json")

		with pytest.raises(StorageValidationError, match="invalid json"):
			StorageGrid.load_from_file(str(bad_file))

	def test_schema_validation_failure(self, tmp_path):
		schema_file = tmp_path / "schema.json"
		schema_file.write_text(json.dumps({"left_module": "not_a_list"}))

		with pytest.raises(StorageValidationError, match="storage schema validation failed"):
			StorageGrid.load_from_file(str(schema_file))

	def test_loads_empty_grid(self, tmp_path):
		f = tmp_path / "empty.json"
		f.write_text(json.dumps(EMPTY_GRID))

		grid = StorageGrid.load_from_file(str(f))

		assert grid.left_module == []
		assert grid.right_module == []

	def test_loads_grid_with_shelves(self, tmp_path):
		f = tmp_path / "shelves.json"
		f.write_text(json.dumps(GRID_WITH_SHELVES))

		grid = StorageGrid.load_from_file(str(f))

		assert len(grid.left_module) == 1
		assert len(grid.left_module[0].cells) == 1
		assert len(grid.right_module) == 1
		assert len(grid.right_module[0].cells) == 2

	def test_requires_explicit_path(self):
		sig = inspect.signature(StorageGrid.load_from_file)
		path_param = sig.parameters["path"]

		assert path_param.default is inspect.Parameter.empty


class TestFilenameField:

	def test_default_is_empty(self):
		grid = StorageGrid()

		assert grid.filename == ""

	def test_explicit_filename_preserved(self):
		grid = StorageGrid(filename="/custom/path.json")

		assert grid.filename == "/custom/path.json"

	def test_filename_not_forced(self):
		validators = StorageGrid.__pydantic_decorators__.validators

		for v in validators:
			assert v.info.func.__name__ != "_force_storage_path"

	def test_filename_from_json_preserved(self, tmp_path):
		data = {**EMPTY_GRID, "filename": "/some/path"}
		f = tmp_path / "with_fn.json"
		f.write_text(json.dumps(data))

		grid = StorageGrid.load_from_file(str(f))

		assert grid.filename == str(f.resolve())


class TestSaveToFile:

	def test_writes_valid_json(self, tmp_path):
		grid = StorageGrid.model_validate(GRID_WITH_SHELVES)
		out = tmp_path / "out.json"

		grid.save_to_file(str(out))

		parsed = json.loads(out.read_text())

		assert "left_module" in parsed
		assert "right_module" in parsed

	def test_save_uses_filename_field(self, tmp_path):
		out = tmp_path / "via_save.json"
		grid = StorageGrid(filename=str(out))

		grid.save()

		assert out.exists() is True

	def test_creates_backup_on_overwrite(self, tmp_path):
		out = tmp_path / "grid.json"
		out.write_text(json.dumps(EMPTY_GRID))

		grid = StorageGrid.model_validate(GRID_WITH_SHELVES)
		grid.save_to_file(str(out))

		backup_dir = tmp_path / "storage"
		backups = list(backup_dir.glob("*.json"))

		assert len(backups) == 1
		assert json.loads(backups[0].read_text()) == EMPTY_GRID

	def test_creates_backup_in_data_storage_directory(self, tmp_path):
		out = tmp_path / "data" / "storage.json"
		out.parent.mkdir(parents=True)
		out.write_text(json.dumps(EMPTY_GRID))

		grid = StorageGrid.model_validate(GRID_WITH_SHELVES)
		grid.save_to_file(str(out))

		backup_dir = tmp_path / "data" / "storage"
		backups = list(backup_dir.glob("*.json"))

		assert len(backups) == 1
		assert json.loads(backups[0].read_text()) == EMPTY_GRID

	def test_no_backup_for_new_file(self, tmp_path):
		out = tmp_path / "brand_new.json"
		grid = StorageGrid()

		grid.save_to_file(str(out))

		all_files = list(tmp_path.iterdir())

		assert len(all_files) == 1
		assert all_files[0].name == "brand_new.json"

	def test_roundtrip(self, tmp_path):
		original = StorageGrid.model_validate(GRID_WITH_SHELVES)
		f = tmp_path / "roundtrip.json"

		original.save_to_file(str(f))
		reloaded = StorageGrid.load_from_file(str(f))

		assert len(reloaded.left_module) == len(original.left_module)
		assert len(reloaded.right_module) == len(original.right_module)
		assert reloaded.right_module[0].cells[0].item is not None
		assert reloaded.right_module[0].cells[0].item.weight == 1.5

	def test_save_to_file_updates_filename_field_and_payload(self, tmp_path):
		out = tmp_path / "saved.json"
		grid = StorageGrid(filename="/stale/path.json")

		grid.save_to_file(str(out))

		assert grid.filename == str(out.resolve())

		payload = json.loads(out.read_text())
		assert payload["filename"] == str(out.resolve())

	def test_write_failure_preserves_existing_file(self, tmp_path):
		out = tmp_path / "grid.json"
		out.write_text(json.dumps(EMPTY_GRID))

		with pytest.raises(TypeError):
			write_json_file_with_backup(
				out,
				{"bad": object()},
				backup_group_name="storage",
			)

		assert json.loads(out.read_text()) == EMPTY_GRID
		assert list(tmp_path.glob(".grid.json.*.tmp")) == []

		backups = list((tmp_path / "storage").glob("*.json"))

		assert len(backups) == 1
		assert json.loads(backups[0].read_text()) == EMPTY_GRID
