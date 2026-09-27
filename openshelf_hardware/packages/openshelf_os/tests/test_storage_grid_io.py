import json

import pytest

from openshelf_os.common import write_json_file_with_backup
from openshelf_os.storage import StorageGrid, StorageValidationError


MINIMAL_CONFIG_DICT = {
	"name": "test",
	"schema_version": 4,
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
		"grid": {"path": "REPLACE_ME"},
		"io": {
			"motors": [],
		},
	},
	"auth": {"require_auth": False},
}



EMPTY_GRID = {"left_module": [], "right_module": []}

GRID_WITH_SHELVES = {
	"left_module": [
		{
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


LEGACY_ITEM = {
	"dimensions": {"length": 80.0, "width": 40.0, "height": 100.0},
	"weight": 350.0,
	"data": {"name": "Bottle"},
	"upca": "880000000001",
}


class TestLegacyUpcaItem:
	"""Grid files written before the upca -> barcode rename kept the item code under "upca"."""

	def _grid_with_item(self, item: dict) -> dict:
		return {
			"left_module": [],
			"right_module": [
				{
					"shelf_width": 400.0,
					"cells": [
						{
							"status": "occupied",
						"item": item,
						"calibration_point": {"x": 30.0, "y": 40.0, "s": 1, "z_offset": 0.0},
						"dimensions": {"length": 100.0, "width": 50.0, "height": 150.0},
					}
				],
			}
		],
		}

	def _load(self, tmp_path, name: str, item: dict):
		f = tmp_path / name
		f.write_text(json.dumps(self._grid_with_item(item)))

		return StorageGrid.load_from_file(str(f)), f

	def _load_item(self, tmp_path, name: str, item: dict):
		grid, _ = self._load(tmp_path, name, item)
		cell_item = grid.right_module[0].cells[0].item

		assert cell_item is not None

		return cell_item

	def test_legacy_upca_key_maps_to_barcode_on_load(self, tmp_path):
		cell_item = self._load_item(tmp_path, "legacy.json", LEGACY_ITEM)

		assert cell_item.barcode == "880000000001"

	def test_modern_barcode_file_unaffected(self, tmp_path):
		item = dict(LEGACY_ITEM)
		del item["upca"]
		item["barcode"] = "880000000001"

		cell_item = self._load_item(tmp_path, "modern.json", item)

		assert cell_item.barcode == "880000000001"

	def test_barcode_key_not_clobbered_when_both_present(self, tmp_path):
		item = dict(LEGACY_ITEM)
		item["barcode"] = "880000000002"

		cell_item = self._load_item(tmp_path, "both.json", item)

		# The modern key is present, so its value is what loads, not the legacy one.
		assert cell_item.barcode == "880000000002"

	def test_legacy_grid_roundtrip_keeps_barcode(self, tmp_path):
		grid, _ = self._load(tmp_path, "legacy.json", LEGACY_ITEM)
		saved_file = tmp_path / "saved.json"

		grid.save_to_file(str(saved_file))
		reloaded = StorageGrid.load_from_file(str(saved_file))
		reloaded_item = reloaded.right_module[0].cells[0].item

		assert reloaded_item is not None
		assert reloaded_item.barcode == "880000000001"

		saved_item = json.loads(saved_file.read_text())["right_module"][0]["cells"][0]["item"]

		assert saved_item["barcode"] == "880000000001"
		assert "upca" not in saved_item


class TestFilenameField:

	def test_filename_field_defaults_and_round_trips(self):
		assert StorageGrid().filename == ""
		assert StorageGrid(filename="/custom/path.json").filename == "/custom/path.json"

	def test_filename_from_json_preserved(self, tmp_path):
		data = {**EMPTY_GRID, "filename": "/some/path"}
		f = tmp_path / "with_fn.json"
		f.write_text(json.dumps(data))

		grid = StorageGrid.load_from_file(str(f))

		assert grid.filename == str(f.resolve())


class TestSaveToFile:

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
		assert backups[0].name.endswith(".production.json")
		assert json.loads(backups[0].read_text()) == EMPTY_GRID

	def test_backup_name_includes_mode_and_shared_version_id(self, tmp_path):
		out = tmp_path / "storage.development.json"
		out.write_text(json.dumps(EMPTY_GRID))

		grid = StorageGrid.model_validate(GRID_WITH_SHELVES)
		grid.save_to_file(str(out), version_id="version-1")

		backup = tmp_path / "storage" / "version-1.development.json"
		assert json.loads(backup.read_text()) == EMPTY_GRID

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
