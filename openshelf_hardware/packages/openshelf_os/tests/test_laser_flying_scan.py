import asyncio

import pytest

from hardware.exceptions import ModbusTimeoutError
from openshelf_os.laser_calibration import (
	scan_axis_flying,
)
import openshelf_os.laser_calibration.flying as flying_module


class FakeAxisMotor:
	def __init__(
		self,
		position_mm: float = 0.0,
		step_mm: float = 1.0,
		max_steps: int | None = None,
	) -> None:
		self.position_mm = float(position_mm)
		self.target_mm = float(position_mm)
		self.step_mm = float(step_mm)
		self.max_steps = max_steps
		self.steps_taken = 0
		self.is_moving = False
		self.moves: list[tuple[float, int | None]] = []
		self.stop_count = 0
		self.read_timeouts_s: list[float | None] = []

	async def move_to_position_no_wait(self, value: float, rpm: int | None = None) -> bool:
		await asyncio.sleep(0)
		self.moves.append((float(value), rpm))
		self.target_mm = float(value)

		if abs(self.target_mm - self.position_mm) <= 0.01:
			return False

		self.is_moving = True
		return True

	async def get_position(self, *, timeout_s: float | None = None) -> float:
		await asyncio.sleep(0)
		self.read_timeouts_s.append(timeout_s)

		if self.is_moving is True:
			if self.max_steps is not None and self.steps_taken >= self.max_steps:
				return float(self.position_mm)

			direction = 1.0 if self.target_mm >= self.position_mm else -1.0
			remaining_mm = abs(self.target_mm - self.position_mm)
			self.position_mm += direction * min(self.step_mm, remaining_mm)
			self.steps_taken += 1

			if abs(self.target_mm - self.position_mm) <= 1e-9:
				self.position_mm = self.target_mm
				self.is_moving = False

		return float(self.position_mm)

	async def stop(self) -> None:
		self.stop_count += 1
		self.is_moving = False
		self.target_mm = self.position_mm


class FakeFlyingTool:
	"""Two gantry motors plus a laser, with the wait path advancing the moving axis."""

	def __init__(self, depth_mm: float | None = 5.0) -> None:
		self._mx = FakeAxisMotor()
		self._my = FakeAxisMotor()
		self.depth_mm = depth_mm
		self.depth_reads = 0
		self.gantry_wait_calls = 0
		self.wait_positions: list[tuple[float, float]] = []
		self.laser_timeouts_s: list[float | None] = []
		# 1-based laser read counts that fail when bounded, or that take the given time to answer.
		self.failing_reads: set[int] = set()
		self.slow_reads: dict[int, float] = {}

	async def get_laser_distance(self, *, timeout_s: float | None = None) -> float | None:
		await asyncio.sleep(0)
		self.depth_reads += 1
		self.laser_timeouts_s.append(timeout_s)

		if self.depth_reads in self.failing_reads and timeout_s is not None:
			raise ModbusTimeoutError("laser did not answer")

		if self.depth_reads in self.slow_reads:
			await asyncio.sleep(self.slow_reads[self.depth_reads])

		return self.depth_mm

	async def gantry_wait(self, timeout_seconds: float = 120.0, should_abort=None) -> None:
		await asyncio.sleep(0)
		self.gantry_wait_calls += 1
		self.wait_positions.append((self._mx.position_mm, self._my.position_mm))
		polls = 0
		last_position = (self._mx.position_mm, self._my.position_mm)

		while self._mx.is_moving is True or self._my.is_moving is True:
			if should_abort is not None and should_abort() is True:
				await self._stop_moving()
				raise asyncio.CancelledError()

			await self._mx.get_position()
			await self._my.get_position()
			polls += 1
			current_position = (self._mx.position_mm, self._my.position_mm)

			if current_position == last_position and polls > 50:
				# The real wait times out on an axis that never completes; emulate that so a stall
				# propagates instead of spinning forever.
				await self._stop_moving()
				raise asyncio.TimeoutError("gantry wait timed out")

			last_position = current_position
			await asyncio.sleep(0)

		if should_abort is not None and should_abort() is True:
			raise asyncio.CancelledError()

	async def _stop_moving(self) -> None:
		for motor in (self._mx, self._my):
			if motor.is_moving is True:
				await motor.stop()


@pytest.mark.asyncio
async def test_scan_axis_flying_stops_the_axis_when_aborted() -> None:
	tool = FakeFlyingTool(depth_mm=5.0)

	def should_abort() -> bool:
		return tool.depth_reads >= 3

	with pytest.raises(asyncio.CancelledError):
		await scan_axis_flying(
			tool=tool,
			axis="y",
			start_mm=0.0,
			end_mm=1000.0,
			fixed_mm=0.0,
			rpm=30,
			progress_label="flying_abort",
			should_abort=should_abort,
		)

	assert tool._my.is_moving is False
	assert tool._my.stop_count >= 1


class FakeGuardInterlock:
	"""Engages once the laser has been read trip_after times, then clears after a few checks."""

	def __init__(self, tool: FakeFlyingTool, trip_after: int, blocked_checks: int) -> None:
		self.tool = tool
		self.trip_after = trip_after
		self.blocked_checks = blocked_checks
		self.has_tripped = False
		self.moving_when_tripped: bool | None = None

	@property
	def blocked(self) -> bool:
		if self.has_tripped is False and self.tool.depth_reads >= self.trip_after:
			self.has_tripped = True
			self.moving_when_tripped = self.tool._my.is_moving

		if self.has_tripped is False or self.blocked_checks <= 0:
			return False

		self.blocked_checks -= 1
		return True


@pytest.mark.asyncio
async def test_scan_axis_flying_stops_and_restarts_the_pass_when_a_guard_engages(
	monkeypatch,
) -> None:
	monkeypatch.setattr(flying_module, "FLYING_SCAN_SAFETY_POLL_S", 0.0)
	tool = FakeFlyingTool(depth_mm=5.0)
	interlock = FakeGuardInterlock(tool, trip_after=3, blocked_checks=5)
	tool._mx._safety = interlock
	tool._my._safety = interlock
	printed: list[str] = []

	line = await scan_axis_flying(
		tool=tool,
		axis="y",
		start_mm=0.0,
		end_mm=20.0,
		fixed_mm=0.0,
		rpm=30,
		progress_label="flying_guard",
		progress_printer=printed.append,
	)

	assert interlock.moving_when_tripped is True
	assert tool._my.stop_count >= 1
	assert any("safety_guard=stopped" in message for message in printed)
	# The guard trips within the resume overlap of start_mm, so the pass restarts from start_mm
	# instead of resuming, and the kept line has no samples from before the stop.
	assert [value for value, _ in tool._my.moves].count(0.0) == 2
	assert line.samples[0].position_mm == pytest.approx(0.0)
	assert line.samples[-1].position_mm == pytest.approx(20.0)
	assert all(
		later.position_mm >= earlier.position_mm
		for earlier, later in zip(line.samples, line.samples[1:])
	)


@pytest.mark.asyncio
async def test_scan_axis_flying_fails_the_line_instead_of_restarting_when_the_stop_fails() -> None:
	tool = FakeFlyingTool(depth_mm=5.0)
	tool.failing_reads = {4}

	async def fail_stop() -> None:
		raise RuntimeError("stop write failed")

	tool._my.stop = fail_stop

	with pytest.raises(flying_module.FlyingScanStopFailed):
		await scan_axis_flying(
			tool=tool,
			axis="y",
			start_mm=0.0,
			end_mm=20.0,
			fixed_mm=0.0,
			rpm=30,
			progress_label="flying_stop_failure",
			progress_printer=lambda _message: None,
		)

	# One flying move and no second pass commanded into an axis that may still be moving.
	assert [rpm for _, rpm in tool._my.moves].count(30) == 1
