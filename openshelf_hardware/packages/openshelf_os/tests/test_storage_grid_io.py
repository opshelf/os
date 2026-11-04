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


class TestSaveToFile:


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
