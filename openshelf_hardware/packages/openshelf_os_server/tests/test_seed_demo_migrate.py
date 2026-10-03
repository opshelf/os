import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import seed_demo  # noqa: E402
from openshelf_os.demo_side_rename import DEMO_SIDE_RENAME_BACKUP_SUFFIX  # noqa: E402
from openshelf_os.storage import StorageGrid, StorageModuleSide  # noqa: E402


PRE_CUTOVER_DEMO = {
	"012345678901": "L:0:0",
	"012345678902": {
		"cell": "LEFT_MODULE:0:1",
		"data": {"name": "Hydrochloric Acid", "weight": 590.0},
	},
	"012345678903": {
		"cell": "RIGHT_MODULE:1:2",
		"data": {"name": "Sodium Hydroxide", "weight": 1200.0},
	},
	"012345678904": "R:2:3",
}


def write_pre_cutover_demo(tmp_path: Path) -> tuple[Path, str]:
	original = json.dumps(PRE_CUTOVER_DEMO)
	demo_path = tmp_path / "demo.json"
	demo_path.write_text(original)
	return demo_path, original


def make_test_grid() -> StorageGrid:
	def make_module() -> list:
		return [
			{
				"shelf_idx": shelf_idx,
				"cells": [
					{
						"cell_idx": cell_idx,
						"status": "available",
						"item": None,
						"calibration_point": None,
						"dimensions": {"width": 50.0, "height": 50.0, "length": 100.0},
					}
					for cell_idx in range(4)
				],
			}
			for shelf_idx in range(3)
		]

	return StorageGrid.model_validate({"front_module": make_module(), "back_module": make_module()})


def test_normalize_demo_payload_migrates_pre_cutover_side_letters(tmp_path) -> None:
	demo_path, original = write_pre_cutover_demo(tmp_path)

	entries = seed_demo.normalize_demo_payload(demo_path)

	assert entries["012345678901"]["cell"] == "F:0:0"
	assert entries["012345678902"]["cell"] == "FRONT_MODULE:0:1"
	assert entries["012345678903"]["cell"] == "BACK_MODULE:1:2"
	assert entries["012345678904"]["cell"] == "B:2:3"

	location = seed_demo.parse_demo_location(entries["012345678901"]["cell"])
	assert location.side == StorageModuleSide.FRONT
	assert (location.shelf_idx, location.cell_idx) == (0, 0)
	location = seed_demo.parse_demo_location(entries["012345678902"]["cell"])
	assert location.side == StorageModuleSide.FRONT
	assert (location.shelf_idx, location.cell_idx) == (0, 1)
	location = seed_demo.parse_demo_location(entries["012345678903"]["cell"])
	assert location.side == StorageModuleSide.BACK
	assert (location.shelf_idx, location.cell_idx) == (1, 2)
	location = seed_demo.parse_demo_location(entries["012345678904"]["cell"])
	assert location.side == StorageModuleSide.BACK
	assert (location.shelf_idx, location.cell_idx) == (2, 3)

	migrated = json.loads(demo_path.read_text(encoding="utf-8"))
	assert migrated["012345678901"] == "F:0:0"
	assert migrated["012345678902"]["cell"] == "FRONT_MODULE:0:1"
	assert migrated["012345678903"]["cell"] == "BACK_MODULE:1:2"
	assert migrated["012345678904"] == "B:2:3"

	backup_path = demo_path.parent / (demo_path.name + DEMO_SIDE_RENAME_BACKUP_SUFFIX)
	assert backup_path.is_file()
	assert backup_path.read_text(encoding="utf-8") == original

	entries_before = sorted(path.name for path in tmp_path.iterdir())
	file_bytes_before = demo_path.read_bytes()
	backup_bytes_before = backup_path.read_bytes()

	second_entries = seed_demo.normalize_demo_payload(demo_path)

	assert second_entries == entries
	assert demo_path.read_bytes() == file_bytes_before
	assert backup_path.read_bytes() == backup_bytes_before
	assert sorted(path.name for path in tmp_path.iterdir()) == entries_before


def test_build_seed_records_resolves_pre_cutover_locations(tmp_path) -> None:
	demo_path, _ = write_pre_cutover_demo(tmp_path)
	entries = seed_demo.normalize_demo_payload(demo_path)
	grid = make_test_grid()

	records, placeholder_count = seed_demo.build_seed_records(grid, entries)

	by_barcode = {record.barcode: record for record in records}
	front = by_barcode["012345678901"].location
	assert front.side == StorageModuleSide.FRONT
	assert (front.shelf_idx, front.cell_idx) == (0, 0)
	front_full = by_barcode["012345678902"].location
	assert front_full.side == StorageModuleSide.FRONT
	assert (front_full.shelf_idx, front_full.cell_idx) == (0, 1)
	back = by_barcode["012345678903"].location
	assert back.side == StorageModuleSide.BACK
	assert (back.shelf_idx, back.cell_idx) == (1, 2)
	back_full = by_barcode["012345678904"].location
	assert back_full.side == StorageModuleSide.BACK
	assert (back_full.shelf_idx, back_full.cell_idx) == (2, 3)

	# The grid has 3 shelves x 4 cells per side; the 4 demo entries occupy 4 cells.
	assert placeholder_count == 2 * 3 * 4 - 4
	assert by_barcode["012345678901"].metadata["is_demo_physical"] is True


def test_normalize_demo_payload_stale_backup_raises(tmp_path) -> None:
	demo_path, _ = write_pre_cutover_demo(tmp_path)
	backup_path = demo_path.parent / (demo_path.name + DEMO_SIDE_RENAME_BACKUP_SUFFIX)
	backup_path.write_text('{"stale": "bytes"}')

	with pytest.raises(ValueError, match="does not match the current file"):
		seed_demo.normalize_demo_payload(demo_path)


def test_parse_demo_location_still_rejects_pre_cutover_letters() -> None:
	for value in ("L:0:1", "R:0:1", "LEFT_MODULE:0:1", "RIGHT_MODULE:0:1"):
		with pytest.raises(ValueError, match="invalid demo location side"):
			seed_demo.parse_demo_location(value)
