from __future__ import annotations

import math


def build_axis_positions(start: float, stop: float, step: float) -> list[float]:
	if step <= 0:
		raise ValueError('step must be positive')

	positions: list[float] = []
	direction = 1.0 if stop >= start else -1.0
	current = float(start)
	limit = float(stop)
	step_value = float(step) * direction
	tolerance = abs(float(step)) * 1e-9

	while (current - limit) * direction <= tolerance:
		positions.append(float(round(current, 10)))
		current += step_value

	if len(positions) == 0 or math.isclose(positions[-1], limit) is False:
		positions.append(limit)

	return positions
