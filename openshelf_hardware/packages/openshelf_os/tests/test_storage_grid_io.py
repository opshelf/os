import json

from pathlib import Path

import pytest

from openshelf_os.common import write_json_file_with_backup
from openshelf_os.storage import StorageGrid


EMPTY_GRID = {"front_module": [], "back_module": []}

GRID_WITH_SHELVES = {
	"front_module": [
		{
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
	"back_module": [
		{
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
			"front_module": [],
			"back_module": [
				{
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
		cell_item = grid.back_module[0].cells[0].item

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
		reloaded_item = reloaded.back_module[0].cells[0].item

		assert reloaded_item is not None
		assert reloaded_item.barcode == "880000000001"

		saved_item = json.loads(saved_file.read_text())["back_module"][0]["cells"][0]["item"]

		assert saved_item["barcode"] == "880000000001"
		assert "upca" not in saved_item


class TestSideRenameMigration:
	"""Storage files written before the front/back cutover keyed their sides as left_module/right_module."""

	def _legacy_file(self, tmp_path, name: str) -> Path:
		f = tmp_path / name
		f.write_text(json.dumps({
			"left_module": [
				{
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
					"cells": [
						{
							"status": "occupied",
							"item": {
								"dimensions": {"length": 80.0, "width": 40.0, "height": 100.0},
								"weight": 1.5,
							},
							"calibration_point": {"x": 30.0, "y": 40.0, "s": 1, "z_offset": 0.0},
							"dimensions": {"length": 100.0, "width": 50.0, "height": 150.0},
						}
					],
				}
			],
		}))
		return f

	def test_legacy_side_keys_migrate_in_place_on_load(self, tmp_path):
		f = self._legacy_file(tmp_path, "legacy.json")
		original = f.read_text()

		grid = StorageGrid.load_from_file(str(f))

		assert len(grid.front_module) == 1
		assert len(grid.back_module) == 1
		assert grid.back_module[0].cells[0].item.weight == 1.5
		payload = json.loads(f.read_text())
		assert "left_module" not in payload
		assert "right_module" not in payload
		assert payload["front_module"] == json.loads(original)["left_module"]
		assert payload["back_module"] == json.loads(original)["right_module"]

	def test_migration_writes_an_exclusive_created_backup(self, tmp_path):
		f = self._legacy_file(tmp_path, "legacy.json")
		original = f.read_text()

		StorageGrid.load_from_file(str(f))

		backup = tmp_path / (f.name + ".side-rename.bak")
		assert backup.exists() is True
		assert backup.read_text() == original

	def test_migration_is_idempotent_on_reload(self, tmp_path):
		f = self._legacy_file(tmp_path, "legacy.json")

		StorageGrid.load_from_file(str(f))
		migrated = f.read_text()

		StorageGrid.load_from_file(str(f))

		assert f.read_text() == migrated
		assert len(list(tmp_path.glob("*.side-rename.bak"))) == 1

	def test_modern_file_is_left_untouched(self, tmp_path):
		f = tmp_path / "modern.json"
		f.write_text(json.dumps(GRID_WITH_SHELVES))
		original = f.read_text()

		StorageGrid.load_from_file(str(f))

		assert f.read_text() == original
		assert list(tmp_path.glob("*.side-rename.bak")) == []


class TestSaveToFile:


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
