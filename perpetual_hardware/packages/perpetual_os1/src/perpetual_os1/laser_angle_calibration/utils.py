from __future__ import annotations

from .constants import GAP_DEPTH_THRESHOLD_MM


def depth_to_label(depth_mm: float | None) -> str:
	if depth_mm is None:
		return 'none'

	return f'{depth_mm:.2f}'


def is_positive_depth(depth_mm: float | None) -> bool:
	if depth_mm is None:
		return False

	return depth_mm > GAP_DEPTH_THRESHOLD_MM
