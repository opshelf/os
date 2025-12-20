"""Shelf schema helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

Coordinate = Tuple[float, float, float]
Coordinate2D = Tuple[float, float]
ShelfOffsets = Sequence[float]
ShelfCoordinates = List[Coordinate]

sl: List[Coordinate2D] = [
    (900.664+6.5, 157.704-1),
    (909.304+6.5, 381.204-1),
    (910.88+6.5, 611.28-1),
    (903.264+6.5, 808.72-1),
    (900.944+6.5, 957.952-1),
    (888.888+6.5, 1108.724-1),
]

sr: List[Coordinate2D] = [
    (874.856-8, 59.992-1),
    (877.008-8, 411.124-1),
    (875.968-8, 709.3+1),
    (846.416-8, 1007.82-1),
    (846.184-8, 1207.96-1),
]


def expand_along_x(
    coordinates: Sequence[Tuple[float, float]],
    offsets: Sequence[ShelfOffsets],
    z_offsets: Sequence[ShelfOffsets] | None = None,
) -> List[ShelfCoordinates]:
    """Apply per-shelf ``offsets`` returning ``(x + offset, y, z)`` tuples."""

    if len(coordinates) != len(offsets):
        raise ValueError("coordinates and offsets must be the same length")

    if z_offsets is None:
        z_offsets = tuple(tuple(0.0 for _ in shelf) for shelf in offsets)
    elif len(z_offsets) != len(offsets):
        raise ValueError("z_offsets must match coordinates length")

    expanded: List[ShelfCoordinates] = []
    for (x, y), shelf_offsets, shelf_z in zip(coordinates, offsets, z_offsets):
        if len(shelf_offsets) != len(shelf_z):
            raise ValueError("offset and z sequences must align per shelf")
        expanded.append([(x + offset, y, z) for offset, z in zip(shelf_offsets, shelf_z)])
    return expanded


SL_OFFSETS: List[ShelfOffsets] = [
    [i * -105.0 for i in range(9)],
    [i * -105.0 for i in range(7)],
    [i * -105.0 for i in range(7)],
    [i * -90.0 for i in range(8)],
    [i * -90.0 for i in range(8)],
    [i * -120.0 for i in range(6)],
]

SR_OFFSETS: List[ShelfOffsets] = [
    [i * -165.0 for i in range(6)],
    [i * -165.0 for i in range(6)],
    [i * -165.0 for i in range(6)],
    [i * -135.0 for i in range(7)],
    [i * -135.0 for i in range(7)],
]

def _linear_range(count: int, start: float = 0.0, stop: float = 3.0) -> List[float]:
    """Generate linearly spaced offsets inclusive of the endpoints."""
    if count <= 1:
        return [start]
    step = (stop - start) / (count - 1)
    return [start + i * step for i in range(count)]


SL_Z_OFFSETS: List[ShelfOffsets] = [[0.0 for _ in shelf] for shelf in SL_OFFSETS]
SR_Z_OFFSETS: List[ShelfOffsets] = [_linear_range(len(shelf)) for shelf in SR_OFFSETS]

sl_full: List[ShelfCoordinates] = expand_along_x(sl, SL_OFFSETS, SL_Z_OFFSETS)
sr_full: List[ShelfCoordinates] = expand_along_x(sr, SR_OFFSETS, SR_Z_OFFSETS)

DEFAULT_CELL_DIMENSIONS = (100.0, 100.0, 250.0)
DEFAULT_SHELF_HEIGHT = 500.0
DEFAULT_SHELF_WIDTH = 500.0
DEFAULT_DIVIDER_WIDTH = 8.0
DEFAULT_STORAGE_FILENAME = "./storage.json"


def _build_cells(shelf_coordinates: Sequence[Coordinate], side: int) -> List[Dict[str, Any]]:
    """Convert coordinates into storage cell records."""

    length, width, height = DEFAULT_CELL_DIMENSIONS
    return [
        {
            "status": "EMPTY",
            "item": None,
            "calibration_point": {"x": x, "y": y, "z": z, "s": side},
            "dimensions": {"length": length, "width": width, "height": height},
            "empty": True,
        }
        for x, y, z in shelf_coordinates
    ]


def _build_module(
    module_coordinates: Sequence[ShelfCoordinates],
    side: int,
) -> List[Dict[str, Any]]:
    """Assemble shelves for a storage module."""

    return [
        {
            "shelf_height": DEFAULT_SHELF_HEIGHT,
            "shelf_width": DEFAULT_SHELF_WIDTH,
            "cells": _build_cells(shelf_coordinates, side),
            "divider_width": DEFAULT_DIVIDER_WIDTH,
        }
        for shelf_coordinates in module_coordinates
    ]


storage_schema: Dict[str, Any] = {
    "left_module": _build_module(sl_full, side=0),
    "right_module": _build_module(sr_full, side=1),
    "filename": DEFAULT_STORAGE_FILENAME,
}


def write_storage_schema(path: str | Path) -> None:
    """Persist the generated storage schema as JSON."""

    target = Path(path)
    target.write_text(json.dumps(storage_schema, indent=2))


write_storage_schema("lb2.json")
