#!/usr/bin/env python3
"""
Generate storage.json for device 68e520ae with custom layout:
- Left module: 12 shelves (3 with 10 cells, 9 with 7 cells)
- Right module: 12 shelves with 10 cells each
- Cell width: 105mm, shelf spacing: 100mm
"""

import json
from typing import Any

BASE_X = 950.0
CELL_SPACING = 105.0
SHELF_SPACING = 100.0
CELL_WIDTH = 105.0
CELL_LENGTH = 105.0
CELL_HEIGHT = 100.0
SHELF_HEIGHT = 100.0
SHELF_WIDTH = 500.0
DIVIDER_WIDTH = 8.0

LEFT_BASE_Y = 20.0
LEFT_BASE_Z = 11.0
RIGHT_BASE_Y = 24.0
RIGHT_BASE_Z = 7.0


def create_cell(x: float, y: float, z_offset: float, side: int) -> dict[str, Any]:
	return {
		"status": "available",
		"item": None,
		"calibration_point": {
			"x": round(x, 3),
			"y": round(y, 3),
			"s": side,
			"z_offset": round(z_offset, 3),
		},
		"dimensions": {
			"length": CELL_LENGTH,
			"width": CELL_WIDTH,
			"height": CELL_HEIGHT,
		},
		"empty": True,
	}


def create_shelf(cells: list[dict[str, Any]]) -> dict[str, Any]:
	return {
		"shelf_height": SHELF_HEIGHT,
		"shelf_width": SHELF_WIDTH,
		"cells": cells,
		"divider_width": DIVIDER_WIDTH,
	}


def generate_left_module() -> list[dict[str, Any]]:
	shelves = []

	for shelf_idx in range(12):
		y = LEFT_BASE_Y + shelf_idx * SHELF_SPACING
		num_cells = 10 if shelf_idx < 3 else 7

		cells = []
		for cell_idx in range(num_cells):
			x = BASE_X - cell_idx * CELL_SPACING
			cells.append(create_cell(x, y, LEFT_BASE_Z, side=0))

		shelves.append(create_shelf(cells))

	return shelves


def generate_right_module() -> list[dict[str, Any]]:
	shelves = []

	for shelf_idx in range(12):
		y = RIGHT_BASE_Y + shelf_idx * SHELF_SPACING
		num_cells = 10

		cells = []
		for cell_idx in range(num_cells):
			x = BASE_X - cell_idx * CELL_SPACING
			cells.append(create_cell(x, y, RIGHT_BASE_Z, side=1))

		shelves.append(create_shelf(cells))

	return shelves


def generate_storage() -> dict[str, Any]:
	return {
		"left_module": generate_left_module(),
		"right_module": generate_right_module(),
		"filename": "/app/perpetual_hardware/packages/perpetual_os1/src/perpetual_os1/storage.json",
	}


def main() -> None:
	storage = generate_storage()

	left_cells = sum(len(shelf["cells"]) for shelf in storage["left_module"])
	right_cells = sum(len(shelf["cells"]) for shelf in storage["right_module"])

	print(f"Left module: {len(storage['left_module'])} shelves, {left_cells} cells")
	print(f"Right module: {len(storage['right_module'])} shelves, {right_cells} cells")
	print(f"Total: {left_cells + right_cells} cells")

	output_path = "storage_68e520ae.json"
	with open(output_path, "w") as f:
		json.dump(storage, f, indent=2)

	print(f"\nGenerated: {output_path}")


if __name__ == "__main__":
	main()
