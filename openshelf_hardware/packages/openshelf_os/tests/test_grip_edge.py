from types import SimpleNamespace

import pytest

from openshelf_os.common import AxisCoordinate
from openshelf_os.laser_calibration.grip_edge import find_grip_edge


class FakeGripTool:
	"""Grips from `edge_mm` on, plus one stray grip a multi-item bin's divider can give."""

	def __init__(self, edge_mm: float, stray_mm: float | None = None) -> None:
		self.edge_mm = edge_mm
		self.stray_mm = stray_mm
		self.z_mm = 0.0
		self.events: list[str] = []
		self._gs = SimpleNamespace(open=self._open, close=self._close)

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


@pytest.mark.asyncio
async def test_grip_edge_ignores_a_stray_grip_and_refines_to_the_first_steady_one() -> None:
	tool = FakeGripTool(edge_mm=6.6, stray_mm=3.0)

	edge_mm = await find_grip_edge(
		tool,
		coordinate=AxisCoordinate(x=500.0, y=440.0, s=0, z_offset=0.0),
		predicted_z_mm=8.0,
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
		predicted_z_mm=8.0,
		label="2:2",
	)

	assert edge_mm is None
