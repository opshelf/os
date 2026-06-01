from __future__ import annotations

from dataclasses import dataclass

from openshelf_os.laser_calibration import shelf_scout as shared_shelf_scout

ShelfSurfaceEdge = shared_shelf_scout.ShelfSurfaceEdge


@dataclass
class StageStallProbeSample:
	rotation_deg: float
	absolute_rotation_deg: float
	probe_x_mm: float
	probe_y_mm: float
	stage_position_mm: float
	stalled: bool
	status: str
	alarm_code: int | None
	pass_name: str = 'coarse'
