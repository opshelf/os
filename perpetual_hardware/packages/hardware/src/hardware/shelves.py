"""Shelf coordinate definitions."""

from __future__ import annotations

from typing import List, Tuple


sl: Tuple[List[float], List[float]] = (
    [900.664, 909.304, 910.88, 903.264, 900.944, 888.888],
    [157.704, 381.204, 611.28, 808.724, 957.952, 1108.724],
)

sr: Tuple[List[float], List[float]] = (
    [874.856, 877.008, 875.968, 846.416, 846.184],
    [59.992, 411.124, 709.3, 1007.82, 1207.96],
)


def pair_coordinates(raw_coordinates: Tuple[List[float], List[float]]) -> List[Tuple[float, float]]:
    """Convert two parallel lists of coordinates into (x, y) tuples."""

    x_values, y_values = raw_coordinates
    return list(zip(x_values, y_values))


sl_pairs: List[Tuple[float, float]] = list((pair_coordinates(sl)))
sr_pairs: List[Tuple[float, float]] = list((pair_coordinates(sr)))
