from types import SimpleNamespace

import pytest

from openshelf_os.common import AxisCoordinate
from openshelf_os.laser_calibration.grip_edge import find_grip_edge, run_grip_edge_sweep
from openshelf_os.storage import StorageModuleSide


class FakeGripTool:
	"""Grips from `edge_mm` on, plus one stray grip a multi-item bin's divider can give."""

	def __init__(self, edge_mm: float, stray_mm: float | None = None) -> None:
		self.edge_mm = edge_mm
		self.stray_mm = stray_mm
		self.z_mm = 0.0
		self.events: list[str] = []
		self._gs = SimpleNamespace(open=self._open, close=self._close, safe_open=self._close)

	async def extend_empty(self, coordinate) -> None:
		self.z_mm = float(coordinate.z_offset)
		self.events.append("extend")

	async def _open(self):
		self.events.append("open")
		gripped = self.z_mm >= self.edge_mm or (self.stray_mm is not None and abs(self.z_mm - self.stray_mm) < 1e-6)
		return SimpleNamespace(gripped=gripped)

	async def _close(self):
		self.events.append("close")

	async def retract_empty(self) -> None:
		self.events.append("retract")

	async def move(self, coordinate) -> None:
		self.events.append(f"move x={coordinate.x:.0f}")

	async def rotate_to_position(self, position) -> None:
		pass


@pytest.mark.asyncio
async def test_grip_edge_ignores_a_stray_grip_and_refines_to_the_first_steady_one() -> None:
	tool = FakeGripTool(edge_mm=6.6, stray_mm=3.0)

	edge_mm = await find_grip_edge(
		tool,
		coordinate=AxisCoordinate(x=500.0, y=440.0, s=0, z_offset=0.0),
		start_z_mm=3.0,
		max_z_mm=9.0,
		label="1:2",
	)

	assert edge_mm == pytest.approx(6.75)
	# The fingers close before every retract, so a probe never pulls the tray out.
	assert all(
		tool.events[index : index + 4] == ["extend", "open", "close", "retract"]
		for index in range(0, len(tool.events), 4)
	)


@pytest.mark.asyncio
async def test_grip_edge_reports_none_when_nothing_grips_steadily() -> None:
	edge_mm = await find_grip_edge(
		FakeGripTool(edge_mm=99.0, stray_mm=7.0),
		coordinate=AxisCoordinate(x=500.0, y=640.0, s=0, z_offset=0.0),
		start_z_mm=3.0,
		max_z_mm=9.0,
		label="2:2",
	)

	assert edge_mm is None


@pytest.mark.asyncio
async def test_grip_edge_sweep_never_moves_to_a_cell_past_the_x_travel() -> None:
	"""lb4-3: the x axis clamps silently, so a cell past its travel would be probed over the divider
	beside the hole and jam a tray; a fresh scan has not marked such cells DISABLED yet."""
	tool = FakeGripTool(edge_mm=6.0)
	analysis = SimpleNamespace(
		shelves=[
			SimpleNamespace(
				shelf_idx=0,
				center_y_mm=40.0,
				cells=[
					SimpleNamespace(shelf_idx=0, cell_idx=0, center_x_mm=200.0, tool_x_mm=210.0, tool_y_mm=40.0),
					SimpleNamespace(shelf_idx=0, cell_idx=1, center_x_mm=500.0, tool_x_mm=510.0, tool_y_mm=40.0),
					SimpleNamespace(shelf_idx=0, cell_idx=2, center_x_mm=950.0, tool_x_mm=960.0, tool_y_mm=40.0),
				],
			)
		]
	)

	result = await run_grip_edge_sweep(
		tool=tool,
		analysis=analysis,
		wall_depths={(0, 0): 9.4, (0, 1): 9.4, (0, 2): 9.4},
		side=StorageModuleSide.FRONT,
		xy_offset=(-2.0, 0.5),
		intercept_mm=-2.5,
		slope_per_mm=0.0,
		preferred_cells=set(),
		excluded_cells=set(),
		x_axis_limit_mm=950.0,
		x_axis_lower_limit_mm=220.0,
	)

	assert [event for event in tool.events if event.startswith("move")] == ["move x=508"]
	assert [(sample.shelf_idx, sample.cell_idx) for sample in result.samples] == [(0, 1)]


@pytest.mark.asyncio
async def test_grip_edge_sweep_stops_at_a_refused_datum_before_moving_on() -> None:
	"""A datum too deep or without a grip refuses the run; probing on first lets the hook push the
	trays of every other cell before that refusal."""
	tool = FakeGripTool(edge_mm=99.0)
	analysis = SimpleNamespace(
		shelves=[
			SimpleNamespace(
				shelf_idx=shelf_idx,
				center_y_mm=40.0 + 200.0 * shelf_idx,
				cells=[
					SimpleNamespace(
						shelf_idx=shelf_idx,
						cell_idx=cell_idx,
						center_x_mm=200.0 + 300.0 * cell_idx,
						tool_x_mm=200.0 + 300.0 * cell_idx,
						tool_y_mm=40.0 + 200.0 * shelf_idx,
					)
					for cell_idx in range(3)
				],
			)
			for shelf_idx in range(2)
		]
	)
	checked = []

	def refuse(sample) -> None:
		checked.append(sample)
		raise ValueError("datum refused")

	with pytest.raises(ValueError, match="datum refused"):
		await run_grip_edge_sweep(
			tool=tool,
			analysis=analysis,
			wall_depths={(shelf_idx, cell_idx): 9.4 for shelf_idx in range(2) for cell_idx in range(3)},
			side=StorageModuleSide.FRONT,
			xy_offset=None,
			intercept_mm=-2.5,
			slope_per_mm=0.0,
			preferred_cells={(0, 0)},
			excluded_cells=set(),
			check_preferred=refuse,
		)

	assert checked == [None]
	assert [event for event in tool.events if event.startswith("move")] == ["move x=200"]
