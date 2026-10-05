import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from openshelf_os.common import LaserScanConfig
from openshelf_os.laser_calibration import (
	COARSE_SCAN_BIN_MEDIAN,
	COARSE_SCAN_BIN_NEAREST,
	COARSE_SCAN_STYLE_FLYING,
	COARSE_SCAN_STYLE_STEPPED,
	FLYING_COARSE_SCAN_RPM,
	FlyingScanLine,
	FlyingScanSample,
	GapCandidateRegion,
	ShelfSurfaceEdge,
	StorageModuleSide,
	bin_samples_to_positions,
	estimate_axis_feed_mm_s,
	estimate_sample_velocity_mm_s,
	find_depth_threshold_crossings,
	get_coarse_scan_bin_mode,
	get_coarse_scan_rpm,
	get_coarse_scan_style,
	get_fine_scan_feed_mm_s,
	get_fine_scan_style,
	GridScanResult,
	resolve_axis_rpm_for_feed,
	sample_depth_position_mm,
	scan_axis_flying,
	scan_line_positions_flying,
	scan_shelf_scout_line,
)
from openshelf_os.laser_calibration.scan import ScanBounds, build_axis_positions
from openshelf_os.laser_calibration.shelf_scout import detect_shelf_edges_from_line
import openshelf_os.laser_calibration.flying as flying_module
import openshelf_os.laser_calibration.shelf_scan as shelf_scan_module


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

	async def move_to_position_no_wait(self, value: float, rpm: int | None = None) -> bool:
		await asyncio.sleep(0)
		self.moves.append((float(value), rpm))
		self.target_mm = float(value)

		if abs(self.target_mm - self.position_mm) <= 0.01:
			return False

		self.is_moving = True
		return True

	async def get_position(self) -> float:
		await asyncio.sleep(0)

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


class FakeTimedAxisMotor(FakeAxisMotor):
	"""Advances toward the target in real time, so velocity estimates see a true feed."""

	def __init__(self, feed_mm_s: float, position_mm: float = 0.0) -> None:
		super().__init__(position_mm=position_mm)
		self.feed_mm_s = float(feed_mm_s)
		self.move_started_at: float | None = None
		self.move_start_mm = float(position_mm)

	async def move_to_position_no_wait(self, value: float, rpm: int | None = None) -> bool:
		did_start = await super().move_to_position_no_wait(value, rpm=rpm)

		if did_start is True:
			self.move_started_at = time.monotonic()
			self.move_start_mm = self.position_mm

		return did_start

	async def get_position(self) -> float:
		await asyncio.sleep(0)

		if self.is_moving is True and self.move_started_at is not None:
			travelled_mm = (time.monotonic() - self.move_started_at) * self.feed_mm_s
			remaining_mm = abs(self.target_mm - self.move_start_mm)

			if travelled_mm >= remaining_mm:
				self.position_mm = self.target_mm
				self.is_moving = False
			else:
				direction = 1.0 if self.target_mm >= self.move_start_mm else -1.0
				self.position_mm = self.move_start_mm + direction * travelled_mm

		return float(self.position_mm)


class FakeFlyingTool:
	"""Two gantry motors plus a laser, with the wait path advancing the moving axis."""

	def __init__(self, depth_mm: float | None = 5.0) -> None:
		self._mx = FakeAxisMotor()
		self._my = FakeAxisMotor()
		self.depth_mm = depth_mm
		self.depth_reads = 0
		self.gantry_wait_calls = 0
		self.wait_positions: list[tuple[float, float]] = []

	async def get_laser_distance(self) -> float | None:
		await asyncio.sleep(0)
		self.depth_reads += 1
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


def test_laser_scan_config_accepts_the_coarse_scan_style_fields() -> None:
	assert LaserScanConfig().coarse_scan_style == COARSE_SCAN_STYLE_STEPPED
	assert LaserScanConfig().coarse_scan_rpm is None
	assert LaserScanConfig().coarse_scan_bin_mode == COARSE_SCAN_BIN_NEAREST

	flying_config = LaserScanConfig.model_validate(
		{
			"coarse_scan_style": "flying",
			"coarse_scan_rpm": 45,
			"coarse_scan_bin_mode": "median",
		}
	)

	assert flying_config.coarse_scan_style == COARSE_SCAN_STYLE_FLYING
	assert flying_config.coarse_scan_rpm == 45
	assert flying_config.coarse_scan_bin_mode == COARSE_SCAN_BIN_MEDIAN

	with pytest.raises(ValidationError):
		LaserScanConfig.model_validate({"coarse_scan_rpm": 0})


def test_coarse_scan_accessors_default_and_validate() -> None:
	assert get_coarse_scan_style(SimpleNamespace()) == COARSE_SCAN_STYLE_STEPPED
	assert get_coarse_scan_rpm(SimpleNamespace()) == FLYING_COARSE_SCAN_RPM
	assert get_coarse_scan_bin_mode(SimpleNamespace()) == COARSE_SCAN_BIN_NEAREST

	assert (
		get_coarse_scan_style(SimpleNamespace(coarse_scan_style=COARSE_SCAN_STYLE_FLYING))
		== COARSE_SCAN_STYLE_FLYING
	)
	assert get_coarse_scan_rpm(SimpleNamespace(coarse_scan_rpm=60)) == 60
	assert (
		get_coarse_scan_bin_mode(SimpleNamespace(coarse_scan_bin_mode=COARSE_SCAN_BIN_MEDIAN))
		== COARSE_SCAN_BIN_MEDIAN
	)

	with pytest.raises(ValueError, match="coarse_scan_style"):
		get_coarse_scan_style(SimpleNamespace(coarse_scan_style="hovering"))

	with pytest.raises(ValueError, match="coarse_scan_rpm"):
		get_coarse_scan_rpm(SimpleNamespace(coarse_scan_rpm=0))

	with pytest.raises(ValueError, match="coarse_scan_bin_mode"):
		get_coarse_scan_bin_mode(SimpleNamespace(coarse_scan_bin_mode="maximum"))

	assert get_fine_scan_style(SimpleNamespace()) == COARSE_SCAN_STYLE_STEPPED
	assert get_fine_scan_style(SimpleNamespace(fine_scan_style="flying")) == COARSE_SCAN_STYLE_FLYING
	assert get_fine_scan_feed_mm_s(SimpleNamespace()) == pytest.approx(40.0)
	assert get_fine_scan_feed_mm_s(SimpleNamespace(fine_scan_feed_mm_s=25.0)) == pytest.approx(25.0)

	with pytest.raises(ValueError, match="fine_scan_style"):
		get_fine_scan_style(SimpleNamespace(fine_scan_style="hovering"))

	with pytest.raises(ValueError, match="fine_scan_feed_mm_s"):
		get_fine_scan_feed_mm_s(SimpleNamespace(fine_scan_feed_mm_s=0.0))


def test_resolve_axis_rpm_for_feed_converts_mm_per_revolution() -> None:
	assert resolve_axis_rpm_for_feed(SimpleNamespace(_params=SimpleNamespace(mpr=80.0)), 40.0) == 30
	assert resolve_axis_rpm_for_feed(SimpleNamespace(_params=SimpleNamespace(mpr=40.0)), 40.0) == 60
	assert resolve_axis_rpm_for_feed(SimpleNamespace(), 40.0) is None
	assert resolve_axis_rpm_for_feed(
		SimpleNamespace(_params=SimpleNamespace(mpr=0.0)),
		40.0,
	) is None


def test_bin_samples_to_positions_picks_nearest_sample_per_position() -> None:
	samples = [
		FlyingScanSample(position_mm=0.4, depth_mm=1.0, elapsed_s=0.0),
		FlyingScanSample(position_mm=0.9, depth_mm=3.0, elapsed_s=0.1),
		FlyingScanSample(position_mm=4.2, depth_mm=2.0, elapsed_s=0.2),
		FlyingScanSample(position_mm=12.4, depth_mm=9.0, elapsed_s=0.3),
		FlyingScanSample(position_mm=20.0, depth_mm=7.0, elapsed_s=0.4),
		FlyingScanSample(position_mm=6.0, depth_mm=None, elapsed_s=0.5),
	]

	assert bin_samples_to_positions(
		samples=samples,
		positions=[0.0, 4.0, 8.0, 12.0],
	) == [1.0, 2.0, None, 9.0]

	assert bin_samples_to_positions(
		samples=samples,
		positions=[0.0, 4.0, 8.0, 12.0],
		mode=COARSE_SCAN_BIN_MEDIAN,
	) == [2.0, 2.0, None, 9.0]


def test_bin_samples_to_positions_handles_descending_positions() -> None:
	values = bin_samples_to_positions(
		samples=[FlyingScanSample(position_mm=98.2, depth_mm=5.0, elapsed_s=0.0)],
		positions=[100.0, 98.0, 96.0],
	)

	assert values == [None, 5.0, None]


def test_bin_samples_to_positions_keeps_gaps_when_sampling_is_too_sparse() -> None:
	values = bin_samples_to_positions(
		samples=[FlyingScanSample(position_mm=30.0, depth_mm=5.0, elapsed_s=0.0)],
		positions=[0.0, 2.0, 4.0],
	)

	assert values == [None, None, None]


def test_bin_samples_to_positions_uses_the_corrected_depth_position() -> None:
	sample = FlyingScanSample(
		position_mm=4.0,
		depth_mm=5.0,
		elapsed_s=0.0,
		depth_position_mm=1.9,
		velocity_mm_s=40.0,
	)

	assert sample_depth_position_mm(sample) == pytest.approx(1.9)
	assert bin_samples_to_positions(samples=[sample], positions=[0.0, 2.0, 4.0]) == [None, 5.0, None]



def test_estimate_sample_velocity_mm_s_uses_trailing_deltas() -> None:
	samples = [
		FlyingScanSample(position_mm=0.0, depth_mm=0.0, elapsed_s=0.0),
		FlyingScanSample(position_mm=0.8, depth_mm=0.0, elapsed_s=0.02),
		FlyingScanSample(position_mm=1.6, depth_mm=0.0, elapsed_s=0.04),
	]

	assert estimate_sample_velocity_mm_s(samples) == pytest.approx(40.0)
	assert estimate_sample_velocity_mm_s(samples[:1]) is None

	descending_samples = [
		FlyingScanSample(position_mm=10.0, depth_mm=0.0, elapsed_s=0.0),
		FlyingScanSample(position_mm=9.2, depth_mm=0.0, elapsed_s=0.02),
	]

	assert estimate_sample_velocity_mm_s(descending_samples) == pytest.approx(-40.0)



def test_find_depth_threshold_crossings_interpolates_both_edges() -> None:
	samples = [
		FlyingScanSample(position_mm=10.0, depth_mm=9.5, elapsed_s=0.0),
		FlyingScanSample(position_mm=16.0, depth_mm=9.5, elapsed_s=0.1),
		FlyingScanSample(position_mm=22.0, depth_mm=9.5, elapsed_s=0.2),
		FlyingScanSample(position_mm=28.0, depth_mm=0.0, elapsed_s=0.3),
		FlyingScanSample(position_mm=34.0, depth_mm=None, elapsed_s=0.4),
		FlyingScanSample(position_mm=40.0, depth_mm=0.0, elapsed_s=0.5),
		FlyingScanSample(position_mm=46.0, depth_mm=9.5, elapsed_s=0.6),
	]

	crossings = find_depth_threshold_crossings(samples=samples, threshold_mm=1.0)

	assert len(crossings) == 2

	falling, rising = crossings
	assert falling.is_rising is False
	assert falling.position_mm == pytest.approx(22.0 + ((1.0 - 9.5) / (0.0 - 9.5)) * 6.0)
	assert rising.is_rising is True
	assert rising.position_mm == pytest.approx(40.0 + ((1.0 - 0.0) / (9.5 - 0.0)) * 6.0)



def test_find_depth_threshold_crossings_uses_corrected_positions() -> None:
	samples = [
		FlyingScanSample(
			position_mm=10.0,
			depth_mm=9.5,
			elapsed_s=0.0,
			depth_position_mm=8.0,
			velocity_mm_s=40.0,
		),
		FlyingScanSample(
			position_mm=16.0,
			depth_mm=0.0,
			elapsed_s=0.1,
			depth_position_mm=14.0,
			velocity_mm_s=40.0,
		),
	]

	crossings = find_depth_threshold_crossings(samples=samples, threshold_mm=1.0)

	assert len(crossings) == 1
	assert crossings[0].position_mm == pytest.approx(14.0 + ((1.0 - 0.0) / (9.5 - 0.0)) * (8.0 - 14.0))


@pytest.mark.asyncio
async def test_scan_axis_flying_samples_until_the_move_finishes() -> None:
	tool = FakeFlyingTool(depth_mm=5.0)

	line = await scan_axis_flying(
		tool=tool,
		axis="y",
		start_mm=0.0,
		end_mm=10.0,
		fixed_mm=25.0,
		rpm=30,
		progress_label="flying_test",
		progress_positions=[0.0, 2.0, 4.0, 6.0, 8.0, 10.0],
	)

	positions = [sample.position_mm for sample in line.samples]

	assert len(positions) >= 2
	assert positions[0] == pytest.approx(0.0)
	assert positions == sorted(positions)
	assert positions[-1] == pytest.approx(10.0)
	assert all(sample.depth_mm == pytest.approx(5.0) for sample in line.samples)
	assert tool._my.moves == [(0.0, None), (10.0, 30)]
	assert tool._mx.moves == [(25.0, None)]
	assert line.rpm == 30
	assert line.axis == "y"
	assert line.fixed_mm == pytest.approx(25.0)
	assert tool.gantry_wait_calls == 2
	assert tool.wait_positions[-1][1] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_scan_axis_flying_corrects_depth_positions_by_capture_latency() -> None:
	tool = FakeFlyingTool(depth_mm=5.0)

	line = await scan_axis_flying(
		tool=tool,
		axis="y",
		start_mm=0.0,
		end_mm=10.0,
		fixed_mm=25.0,
		rpm=30,
		progress_label="flying_latency",
		capture_latency_s=0.1,
	)

	corrected_samples = [
		sample for sample in line.samples if sample.depth_position_mm is not None
	]

	assert len(corrected_samples) > 0

	for sample in corrected_samples:
		assert sample.velocity_mm_s is not None
		assert sample.depth_position_mm == pytest.approx(
			sample.position_mm - sample.velocity_mm_s * 0.1
		)

	# The stationary start sample has no velocity estimate, so it is left where it was measured.
	assert line.samples[0].depth_position_mm is None


@pytest.mark.asyncio
async def test_scan_axis_flying_flies_a_lead_out_past_the_last_grid_position() -> None:
	tool = FakeFlyingTool(depth_mm=5.0)
	tool._my._params = SimpleNamespace(mpr=40.0)

	line = await scan_axis_flying(
		tool=tool,
		axis="y",
		start_mm=0.0,
		end_mm=10.0,
		fixed_mm=25.0,
		rpm=30,
		progress_label="flying_lead_out",
		capture_latency_s=0.042,
	)

	# 40mm/rev at 30rpm is 20mm/s, so the pass flies the 0.84mm latency shift plus the 1mm
	# margin past the grid; the corrected samples then still cover the final grid position.
	expected_motion_end_mm = 10.0 + 20.0 * 0.042 + 1.0

	assert tool._my.moves[-1] == (pytest.approx(expected_motion_end_mm), 30)
	assert line.samples[-1].position_mm == pytest.approx(expected_motion_end_mm)
	assert line.samples[-1].position_mm > 10.0
	assert line.start_mm == pytest.approx(0.0)
	assert line.end_mm == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_scan_axis_flying_breaks_when_the_axis_stalls(monkeypatch) -> None:
	import openshelf_os.laser_calibration.flying as flying_module

	monkeypatch.setattr(flying_module, "FLYING_SCAN_STALL_S", 0.01)
	tool = FakeFlyingTool(depth_mm=5.0)
	tool._my.max_steps = 2

	with pytest.raises(asyncio.TimeoutError):
		await scan_axis_flying(
			tool=tool,
			axis="y",
			start_mm=0.0,
			end_mm=100.0,
			fixed_mm=0.0,
			rpm=30,
			progress_label="flying_stall",
		)

	assert tool._my.is_moving is False


@pytest.mark.asyncio
async def test_scan_axis_flying_keeps_sampling_when_reads_move_less_than_the_epsilon(
	monkeypatch,
) -> None:
	import openshelf_os.laser_calibration.flying as flying_module

	class SlowLaserTool(FakeFlyingTool):
		async def get_laser_distance(self) -> float | None:
			await asyncio.sleep(0.001)
			return await super().get_laser_distance()

	monkeypatch.setattr(flying_module, "FLYING_SCAN_START_STALL_S", 0.02)
	tool = SlowLaserTool(depth_mm=5.0)
	# Each read advances well under FLYING_SCAN_PROGRESS_EPS_MM, as a slow feed does.
	tool._my.step_mm = 0.005

	line = await scan_axis_flying(
		tool=tool,
		axis="y",
		start_mm=0.0,
		end_mm=1.0,
		fixed_mm=0.0,
		rpm=30,
		progress_label="flying_slow",
	)

	moving_positions = [sample.position_mm for sample in line.samples[:-1]]

	assert max(moving_positions) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_scan_line_positions_flying_bins_the_moving_samples() -> None:
	tool = FakeFlyingTool(depth_mm=7.5)

	depth_values, line = await scan_line_positions_flying(
		tool,
		axis="x",
		positions=[0.0, 2.0, 4.0, 6.0, 8.0, 10.0],
		fixed_mm=40.0,
		rpm=45,
		progress_label="flying_wrapper",
		capture_latency_s=0.0,
	)

	assert depth_values == [pytest.approx(7.5) for _ in range(6)]
	assert line.rpm == 45
	assert line.axis == "x"


def test_bin_samples_to_positions_fills_the_final_bin_with_the_lead_out() -> None:
	from openshelf_os.laser_calibration.flying import FLYING_SCAN_LEAD_OUT_MARGIN_MM

	positions = [float(value) for value in range(0, 21)]
	feed_mm_s = 40.0
	correction_mm = feed_mm_s * 0.042
	spacing_mm = 0.4

	def flying_samples_until(last_raw_mm: float) -> list[FlyingScanSample]:
		samples = []
		index = 0

		while index * spacing_mm <= last_raw_mm + 1e-9:
			raw_mm = index * spacing_mm
			samples.append(
				FlyingScanSample(
					position_mm=raw_mm,
					depth_mm=5.0,
					elapsed_s=raw_mm / feed_mm_s,
					depth_position_mm=raw_mm - correction_mm,
					velocity_mm_s=feed_mm_s,
				)
			)
			index += 1

		return samples

	# Before the lead-out the move stopped at end_mm, so the corrected sample cloud ends
	# correction_mm short of it and the final grid position has no sample to bin from.
	without_lead_out = bin_samples_to_positions(
		samples=flying_samples_until(20.0),
		positions=positions,
	)
	assert without_lead_out[-1] is None

	# Flying the latency shift plus the margin past end_mm puts corrected samples past the
	# final grid position, so every bin the stepped scan would have probed is filled.
	with_lead_out = bin_samples_to_positions(
		samples=flying_samples_until(20.0 + correction_mm + FLYING_SCAN_LEAD_OUT_MARGIN_MM),
		positions=positions,
	)
	assert with_lead_out == [pytest.approx(5.0) for _ in positions]


@pytest.mark.asyncio
async def test_scan_line_positions_flying_covers_the_final_position_with_a_clamped_lead_out() -> None:
	tool = FakeFlyingTool(depth_mm=5.0)
	# 40mm/rev at 60rpm is 40mm/s, so the lead-out would fly 2.68mm past the grid; the axis
	# limit at 10mm clamps the commanded end back onto the final grid position instead.
	tool._my = FakeTimedAxisMotor(feed_mm_s=40.0)
	tool._my._params = SimpleNamespace(mpr=40.0, lower_lim=0.0, lim=10.0)
	positions = [float(value) for value in range(0, 11)]

	depth_values, line = await scan_line_positions_flying(
		tool,
		axis="y",
		positions=positions,
		fixed_mm=25.0,
		rpm=60,
		progress_label="flying_clamped_lead_out",
		capture_latency_s=0.042,
	)

	assert tool._my.moves[-1] == (pytest.approx(10.0), 60)
	# The latency-corrected moving samples stop 1.68mm short of the clamped end, so only the
	# stationary end sample can fill the final grid position's bin.
	assert depth_values[-1] is not None
	assert depth_values[-1] == pytest.approx(5.0)
	assert line.samples[-1].position_mm == pytest.approx(10.0)
	assert line.samples[-1].velocity_mm_s == 0.0


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
	# The interrupted pass is discarded: the kept line starts again from start_mm and has no
	# samples from before the stop.
	assert [value for value, _ in tool._my.moves].count(0.0) == 2
	assert line.samples[0].position_mm == pytest.approx(0.0)
	assert line.samples[-1].position_mm == pytest.approx(20.0)
	assert all(
		later.position_mm >= earlier.position_mm
		for earlier, later in zip(line.samples, line.samples[1:])
	)


@pytest.mark.asyncio
async def test_scan_axis_flying_resets_a_stateful_stop_predicate_on_restart(monkeypatch) -> None:
	from openshelf_os.laser_calibration.shelf_scout import build_shelf_edge_stop_predicate

	monkeypatch.setattr(flying_module, "FLYING_SCAN_SAFETY_POLL_S", 0.0)

	class SequencedDepthTool(FakeFlyingTool):
		async def get_laser_distance(self) -> float | None:
			await asyncio.sleep(0)
			self.depth_reads += 1
			# Positive through the interrupted pass, then non-positive right after the restart.
			return 9.5 if self.depth_reads <= 10 else 0.0

	tool = SequencedDepthTool(depth_mm=9.5)
	interlock = FakeGuardInterlock(tool, trip_after=9, blocked_checks=5)
	tool._mx._safety = interlock
	tool._my._safety = interlock
	stop_when = build_shelf_edge_stop_predicate(min_positive_run_length=3, grid_step_mm=2.0)

	line = await scan_axis_flying(
		tool=tool,
		axis="y",
		start_mm=0.0,
		end_mm=20.0,
		fixed_mm=0.0,
		rpm=30,
		progress_label="flying_predicate_reset",
		stop_when=stop_when,
	)

	# The interrupted pass's run spanned ~7mm. The restarted pass's first moving sample is
	# non-positive, so it would confirm that stale run and stop the line at ~1mm if the
	# predicate's state survived the restart.
	assert interlock.has_tripped is True
	assert line.samples[-1].position_mm == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_scan_axis_flying_waits_for_the_guard_to_clear_until_aborted(monkeypatch) -> None:
	monkeypatch.setattr(flying_module, "FLYING_SCAN_SAFETY_POLL_S", 0.0)
	tool = FakeFlyingTool(depth_mm=5.0)
	interlock = FakeGuardInterlock(tool, trip_after=3, blocked_checks=10_000)
	tool._my._safety = interlock

	def should_abort() -> bool:
		return interlock.blocked_checks < 9_000

	with pytest.raises(asyncio.CancelledError):
		await scan_axis_flying(
			tool=tool,
			axis="y",
			start_mm=0.0,
			end_mm=1000.0,
			fixed_mm=0.0,
			rpm=30,
			progress_label="flying_guard_abort",
			should_abort=should_abort,
		)

	assert tool._my.is_moving is False


class FakeVelocityAxisMotor(FakeAxisMotor):
	"""Records Velocity writes the way Motor persists a move's rpm in the drive register."""

	def __init__(self, configured_rpm: int) -> None:
		super().__init__()
		self._params = SimpleNamespace(rpm=configured_rpm)
		self.velocity_rpm = configured_rpm
		self.velocity_writes: list[int] = []

	async def move_to_position_no_wait(self, value: float, rpm: int | None = None) -> bool:
		if rpm is not None:
			await self.set_param("Velocity", rpm)

		return await super().move_to_position_no_wait(value, rpm=rpm)

	async def set_param(self, register: str, value: int) -> None:
		assert register == "Velocity"
		self.velocity_rpm = int(value)
		self.velocity_writes.append(int(value))


@pytest.mark.asyncio
async def test_scan_axis_flying_restores_the_configured_velocity_after_the_pass() -> None:
	tool = FakeFlyingTool(depth_mm=5.0)
	tool._my = FakeVelocityAxisMotor(configured_rpm=300)

	await scan_axis_flying(
		tool=tool,
		axis="y",
		start_mm=0.0,
		end_mm=20.0,
		fixed_mm=0.0,
		rpm=30,
		progress_label="flying_velocity",
	)

	assert tool._my.velocity_writes == [30, 300]
	assert tool._my.velocity_rpm == 300


@pytest.mark.asyncio
async def test_scan_axis_flying_restores_the_configured_velocity_when_aborted() -> None:
	tool = FakeFlyingTool(depth_mm=5.0)
	tool._my = FakeVelocityAxisMotor(configured_rpm=300)

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
			progress_label="flying_velocity_abort",
			should_abort=should_abort,
		)

	assert tool._my.velocity_rpm == 300


@pytest.mark.asyncio
async def test_scan_axis_flying_restores_the_configured_velocity_after_an_early_stop() -> None:
	tool = FakeFlyingTool(depth_mm=5.0)
	tool._my = FakeVelocityAxisMotor(configured_rpm=300)

	await scan_axis_flying(
		tool=tool,
		axis="y",
		start_mm=0.0,
		end_mm=100.0,
		fixed_mm=0.0,
		rpm=30,
		progress_label="flying_velocity_stop_when",
		stop_when=lambda samples: len(samples) >= 3,
	)

	assert tool._my.velocity_rpm == 300


def test_estimate_axis_feed_mm_s_uses_the_axis_mm_per_revolution() -> None:
	assert estimate_axis_feed_mm_s(
		SimpleNamespace(_params=SimpleNamespace(mpr=40.0)),
		30,
	) == pytest.approx(20.0)
	assert estimate_axis_feed_mm_s(
		SimpleNamespace(_params=SimpleNamespace(mpr=80.0)),
		30,
	) == pytest.approx(40.0)
	assert estimate_axis_feed_mm_s(SimpleNamespace(), 30) is None


@pytest.mark.asyncio
async def test_scan_axis_flying_refuses_a_line_that_cannot_finish_in_the_wait_cap() -> None:
	tool = FakeFlyingTool(depth_mm=5.0)
	tool._my._params = SimpleNamespace(mpr=40.0)

	with pytest.raises(ValueError, match="capped"):
		await scan_axis_flying(
			tool=tool,
			axis="y",
			start_mm=0.0,
			end_mm=1000.0,
			fixed_mm=0.0,
			rpm=10,
			progress_label="flying_too_slow",
		)

	assert tool._my.is_moving is False
	assert tool._my.moves == []


@pytest.mark.asyncio
async def test_detect_shelf_edges_matches_the_stepped_scanner(monkeypatch) -> None:
	y_positions = [float(y_mm) for y_mm in range(200, 38, -2)]
	depths = {y_mm: 5.0 for y_mm in range(170, 161, -2)}
	depths[110.0] = 5.0
	depths.update({y_mm: 5.0 for y_mm in range(60, 55, -2)})
	depth_values = [depths.get(y_mm) for y_mm in y_positions]
	current_y_mm = y_positions[0]

	async def didMoveY(y_mm: float) -> None:
		nonlocal current_y_mm
		current_y_mm = float(y_mm)

	async def didSampleLaserDepth(*args, **kwargs) -> float | None:
		return depth_values[y_positions.index(current_y_mm)]

	monkeypatch.setattr(shelf_scan_module, "sample_laser_depth", didSampleLaserDepth)

	tool = SimpleNamespace(
		_mx=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_my=SimpleNamespace(move_to_position_no_wait=AsyncMock(side_effect=didMoveY)),
		gantry_wait=AsyncMock(),
	)

	_, stepped_edges = await scan_shelf_scout_line(
		tool,
		x_mm=25.0,
		y_positions=y_positions,
		settle_ms=0,
		samples_per_point=1,
		progress_label="shelf_scout_equivalence",
		post_edge_skip_mm=40.0,
	)

	flying_edges = detect_shelf_edges_from_line(
		x_mm=25.0,
		y_positions=y_positions,
		depth_values=depth_values,
		post_edge_skip_mm=40.0,
	)

	assert flying_edges == stepped_edges
	assert flying_edges == [
		ShelfSurfaceEdge(x_mm=25.0, y_mm=153.0),
		ShelfSurfaceEdge(x_mm=25.0, y_mm=47.0),
	]


@pytest.mark.asyncio
async def test_capture_shelf_candidates_flying_style_uses_the_flying_scan(
	monkeypatch,
	tmp_path: Path,
) -> None:
	line = FlyingScanLine(
		axis="y",
		fixed_mm=40.0,
		start_mm=100.0,
		end_mm=0.0,
		rpm=45,
		samples=[FlyingScanSample(position_mm=100.0, depth_mm=5.0, elapsed_s=0.0)],
		duration_s=1.0,
	)
	scout_depth_values = [None for _ in build_axis_positions(0.0, 100.0, 2.0)]
	sweep_depth_values = [None for _ in build_axis_positions(0.0, 40.0, 4.0)]
	scout = AsyncMock(
		return_value=(
			scout_depth_values,
			[ShelfSurfaceEdge(x_mm=40.0, y_mm=90.0)],
			line,
		)
	)
	sweep = AsyncMock(return_value=(sweep_depth_values, line))
	stepped = AsyncMock(side_effect=AssertionError("stepped scan used while flying"))

	monkeypatch.setattr(shelf_scan_module, "scan_shelf_scout_line_flying", scout)
	monkeypatch.setattr(shelf_scan_module, "scan_line_positions_flying", sweep)
	monkeypatch.setattr(shelf_scan_module, "scan_shelf_scout_line", stepped)
	monkeypatch.setattr(shelf_scan_module, "scan_line_positions", stepped)
	monkeypatch.setattr(shelf_scan_module, "sample_scan_point", AsyncMock(return_value=None))

	tool = SimpleNamespace(
		_my=SimpleNamespace(
			get_brake_output_diagnostics=AsyncMock(
				return_value={"register_function": 0x24, "output_state": 0x0000}
			)
		)
	)
	recorder = SimpleNamespace(
		run_id="flying-test-run",
		coarse_depth_path=tmp_path / "coarse_depth.npy",
		progress_path=tmp_path / "progress.json",
		run_dir=tmp_path,
	)

	coarse_scan, wall_depth_mm, candidates, shelf_edges = await shelf_scan_module.capture_shelf_candidates(
		tool,
		side=StorageModuleSide.FRONT,
		bounds=ScanBounds(x_min=0.0, x_max=40.0, y_min=0.0, y_max=100.0),
		settle_ms=0,
		samples_per_point=1,
		scan_config=SimpleNamespace(
			coarse_scan_style=COARSE_SCAN_STYLE_FLYING,
			coarse_scan_rpm=45,
			coarse_scan_bin_mode=COARSE_SCAN_BIN_NEAREST,
		),
		recorder=recorder,
		shelf_scout_only=False,
		known_shelf_edge_y=None,
	)

	assert scout.await_count == 1
	assert sweep.await_count >= 1
	assert stepped.await_count == 0
	assert wall_depth_mm == pytest.approx(0.0)
	assert candidates == []
	assert shelf_edges[0].y_mm == pytest.approx(90.0)
	assert len(coarse_scan.depth_rows) > 0
	assert (tmp_path / "flying_samples.jsonl").exists()


@pytest.mark.asyncio
async def test_capture_shelf_candidates_resamples_uncovered_flying_sweep_bins(
	monkeypatch,
	tmp_path: Path,
) -> None:
	sweep_x_positions = build_axis_positions(0.0, 100.0, 4.0)
	opening_indices = range(10, 17)
	missing_idx = 13
	sweep_depth_values: list[float | None] = [
		0.0 if idx in opening_indices else 30.0
		for idx in range(len(sweep_x_positions))
	]
	sweep_depth_values[missing_idx] = None
	line = FlyingScanLine(
		axis="x",
		fixed_mm=90.0,
		start_mm=0.0,
		end_mm=100.0,
		rpm=45,
		samples=[],
		duration_s=1.0,
	)
	scout = AsyncMock(
		return_value=(
			[None for _ in build_axis_positions(0.0, 100.0, 2.0)],
			[ShelfSurfaceEdge(x_mm=40.0, y_mm=90.0)],
			line,
		)
	)
	sweep = AsyncMock(side_effect=lambda *args, **kwargs: (list(sweep_depth_values), line))
	resample_requests: list[float] = []

	async def didSampleStepped(tool, *, x_mm, y_mm, **kwargs):
		resample_requests.append(x_mm)
		return 0.0

	monkeypatch.setattr(shelf_scan_module, "scan_shelf_scout_line_flying", scout)
	monkeypatch.setattr(shelf_scan_module, "scan_line_positions_flying", sweep)
	monkeypatch.setattr(shelf_scan_module, "sample_scan_point", didSampleStepped)

	tool = SimpleNamespace(
		_my=SimpleNamespace(
			get_brake_output_diagnostics=AsyncMock(
				return_value={"register_function": 0x24, "output_state": 0x0000}
			)
		)
	)
	recorder = SimpleNamespace(
		run_id="flying-test-run",
		coarse_depth_path=tmp_path / "coarse_depth.npy",
		progress_path=tmp_path / "progress.json",
		run_dir=tmp_path,
	)

	_, _, candidates, _ = await shelf_scan_module.capture_shelf_candidates(
		tool,
		side=StorageModuleSide.FRONT,
		bounds=ScanBounds(x_min=0.0, x_max=100.0, y_min=0.0, y_max=100.0),
		settle_ms=0,
		samples_per_point=1,
		scan_config=SimpleNamespace(
			coarse_scan_style=COARSE_SCAN_STYLE_FLYING,
			coarse_scan_rpm=45,
			coarse_scan_bin_mode=COARSE_SCAN_BIN_NEAREST,
		),
		recorder=recorder,
		shelf_scout_only=False,
		known_shelf_edge_y=None,
	)

	assert sweep.await_count >= 1
	assert resample_requests == [sweep_x_positions[missing_idx]] * sweep.await_count
	assert len(candidates) == sweep.await_count

	for candidate in candidates:
		assert candidate.x_min == pytest.approx(sweep_x_positions[opening_indices[0]])
		assert candidate.x_max == pytest.approx(sweep_x_positions[opening_indices[-1]])


@pytest.mark.asyncio
async def test_capture_shelf_candidates_default_style_keeps_the_stepped_scan(
	monkeypatch,
	tmp_path: Path,
) -> None:
	scout_depth_values = [None for _ in build_axis_positions(0.0, 100.0, 2.0)]
	sweep_depth_values = [None for _ in build_axis_positions(0.0, 40.0, 4.0)]
	stepped_scout = AsyncMock(return_value=(scout_depth_values, []))
	stepped_sweep = AsyncMock(return_value=sweep_depth_values)
	flying = AsyncMock(side_effect=AssertionError("flying scan used while stepped"))

	monkeypatch.setattr(shelf_scan_module, "scan_shelf_scout_line", stepped_scout)
	monkeypatch.setattr(shelf_scan_module, "scan_line_positions", stepped_sweep)
	monkeypatch.setattr(shelf_scan_module, "scan_shelf_scout_line_flying", flying)
	monkeypatch.setattr(shelf_scan_module, "scan_line_positions_flying", flying)

	tool = SimpleNamespace(
		_my=SimpleNamespace(
			get_brake_output_diagnostics=AsyncMock(
				return_value={"register_function": 0x24, "output_state": 0x0000}
			)
		)
	)
	recorder = SimpleNamespace(
		run_id="flying-test-run",
		coarse_depth_path=tmp_path / "coarse_depth.npy",
		progress_path=tmp_path / "progress.json",
		run_dir=tmp_path,
	)

	_, _, _, shelf_edges = await shelf_scan_module.capture_shelf_candidates(
		tool,
		side=StorageModuleSide.FRONT,
		bounds=ScanBounds(x_min=0.0, x_max=40.0, y_min=0.0, y_max=100.0),
		settle_ms=0,
		samples_per_point=1,
		scan_config=SimpleNamespace(),
		recorder=recorder,
		shelf_scout_only=False,
		known_shelf_edge_y=None,
	)

	assert stepped_scout.await_count == 1
	assert stepped_sweep.await_count >= 1
	assert flying.await_count == 0
	assert shelf_edges[-1].y_mm == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_probe_line_from_center_flying_prefetch_matches_stepped(monkeypatch) -> None:
	import openshelf_os.laser_calibration.fine_scan as fine_scan_module
	import openshelf_os.laser_calibration.flying as flying_module

	step_mm = 2.0
	positions = [float(value) for value in range(0, 50, 2)]
	hole_positions = {float(value) for value in range(20, 46, 2)}
	profile = [0.0 if position in hole_positions else 9.5 for position in positions]
	center_idx = positions.index(32.0)

	async def didSampleStepped(tool, *, x_mm, y_mm, settle_ms, samples_per_point, should_abort=None):
		return profile[positions.index(x_mm)]

	monkeypatch.setattr(fine_scan_module, "sample_scan_point", didSampleStepped)

	stepped_scan = GridScanResult(
		x_positions=positions,
		y_positions=[10.0],
		depth_rows=[[None for _ in positions]],
	)
	stepped_segment = await fine_scan_module.probe_line_from_center(
		SimpleNamespace(),
		base_scan=stepped_scan,
		axis="x",
		positions=positions,
		fixed_mm=10.0,
		center_idx=center_idx,
		step_mm=step_mm,
		progress_prefix="stepped_probe",
		settle_ms=0,
		samples_per_point=1,
		wall_depth_mm=9.5,
		expected_span_mm=26.0,
	)

	assert stepped_segment is not None

	async def didFailToSample(*args, **kwargs):
		raise AssertionError("stepped sampling used while flying")

	monkeypatch.setattr(fine_scan_module, "sample_scan_point", didFailToSample)

	flying_requests: list[dict] = []
	flying_lines: list[FlyingScanLine] = []

	async def didScanLineFlying(
		tool,
		*,
		axis,
		positions,
		fixed_mm,
		rpm,
		progress_label,
		should_abort=None,
		**kwargs,
	):
		flying_requests.append({"axis": axis, "rpm": rpm, "label": progress_label})
		flying_line = FlyingScanLine(
			axis=axis,
			fixed_mm=fixed_mm,
			start_mm=float(positions[0]),
			end_mm=float(positions[-1]),
			rpm=rpm,
			samples=[
				FlyingScanSample(position_mm=position_mm, depth_mm=depth_mm, elapsed_s=index * 0.02)
				for index, (position_mm, depth_mm) in enumerate(zip(positions, profile))
			],
			duration_s=1.0,
		)
		flying_lines.append(flying_line)
		return list(profile), flying_line

	monkeypatch.setattr(flying_module, "scan_line_positions_flying", didScanLineFlying)

	flying_scan = GridScanResult(
		x_positions=positions,
		y_positions=[10.0],
		depth_rows=[[None for _ in positions]],
	)
	recorded_lines: list[str] = []
	flying_segment = await fine_scan_module.probe_line_from_center(
		SimpleNamespace(_mx=SimpleNamespace(_params=SimpleNamespace(mpr=80.0))),
		base_scan=flying_scan,
		axis="x",
		positions=positions,
		fixed_mm=10.0,
		center_idx=center_idx,
		step_mm=step_mm,
		progress_prefix="flying_probe",
		settle_ms=0,
		samples_per_point=1,
		wall_depth_mm=9.5,
		expected_span_mm=26.0,
		scan_config=SimpleNamespace(fine_scan_style="flying", fine_scan_feed_mm_s=40.0),
		on_flying_line=lambda label, line: recorded_lines.append(label),
	)

	assert flying_segment is not None
	assert flying_segment.start_mm == pytest.approx(stepped_segment.start_mm)
	assert flying_segment.end_mm == pytest.approx(stepped_segment.end_mm)
	assert flying_segment.center_mm == pytest.approx(stepped_segment.center_mm)
	assert flying_segment.width_mm == pytest.approx(stepped_segment.width_mm)
	assert flying_requests == [{"axis": "x", "rpm": 30, "label": "flying_probe_flying"}]
	assert recorded_lines == ["flying_probe_flying"]


@pytest.mark.asyncio
async def test_probe_line_from_center_resamples_uncovered_flying_bins(monkeypatch) -> None:
	import openshelf_os.laser_calibration.fine_scan as fine_scan_module
	import openshelf_os.laser_calibration.flying as flying_module

	step_mm = 2.0
	positions = [float(value) for value in range(0, 50, 2)]
	hole_positions = {float(value) for value in range(20, 46, 2)}
	profile = [0.0 if position in hole_positions else 9.5 for position in positions]
	center_idx = positions.index(32.0)
	uncovered_position_mm = 22.0
	prefetched = [
		None if position_mm == uncovered_position_mm else depth_mm
		for position_mm, depth_mm in zip(positions, profile)
	]
	stepped_requests: list[float] = []

	async def didSampleStepped(tool, *, x_mm, y_mm, settle_ms, samples_per_point, should_abort=None):
		stepped_requests.append(x_mm)
		return profile[positions.index(x_mm)]

	monkeypatch.setattr(fine_scan_module, "sample_scan_point", didSampleStepped)

	async def didScanLineFlying(
		tool,
		*,
		axis,
		positions,
		fixed_mm,
		rpm,
		progress_label,
		should_abort=None,
		**kwargs,
	):
		return list(prefetched), FlyingScanLine(
			axis=axis,
			fixed_mm=fixed_mm,
			start_mm=float(positions[0]),
			end_mm=float(positions[-1]),
			rpm=rpm,
			samples=[
				FlyingScanSample(position_mm=position_mm, depth_mm=depth_mm, elapsed_s=index * 0.02)
				for index, (position_mm, depth_mm) in enumerate(zip(positions, profile))
			],
			duration_s=1.0,
		)

	monkeypatch.setattr(flying_module, "scan_line_positions_flying", didScanLineFlying)

	flying_scan = GridScanResult(
		x_positions=positions,
		y_positions=[10.0],
		depth_rows=[[None for _ in positions]],
	)
	segment = await fine_scan_module.probe_line_from_center(
		SimpleNamespace(_mx=SimpleNamespace(_params=SimpleNamespace(mpr=80.0))),
		base_scan=flying_scan,
		axis="x",
		positions=positions,
		fixed_mm=10.0,
		center_idx=center_idx,
		step_mm=step_mm,
		progress_prefix="flying_resample",
		settle_ms=0,
		samples_per_point=1,
		wall_depth_mm=9.5,
		expected_span_mm=26.0,
		scan_config=SimpleNamespace(fine_scan_style="flying", fine_scan_feed_mm_s=40.0),
	)

	# The hole spans 20..44mm. The empty bin at 22mm must not be read as wall, which would
	# truncate the segment's near edge to 24mm and shift its centre by 2mm.
	assert segment is not None
	assert segment.start_mm == pytest.approx(19.0)
	assert segment.end_mm == pytest.approx(45.0)
	assert stepped_requests == [uncovered_position_mm]


@pytest.mark.asyncio
async def test_probe_line_from_center_rebuilds_the_inferred_far_edge_from_the_refined_near_edge(
	monkeypatch,
) -> None:
	import openshelf_os.laser_calibration.fine_scan as fine_scan_module
	import openshelf_os.laser_calibration.flying as flying_module

	step_mm = 2.0
	positions = [float(value) for value in range(0, 50, 2)]
	# The topmost opening runs off the end of the window, so its far edge has to be inferred.
	profile = [0.0 if position >= 20.0 else 9.5 for position in positions]
	center_idx = positions.index(40.0)
	refined_near_edge_mm = 19.25
	samples = [
		FlyingScanSample(
			position_mm=0.5 * index,
			depth_mm=9.5 if 0.5 * index < refined_near_edge_mm else 0.0,
			elapsed_s=index * 0.01,
		)
		for index in range(0, 97)
	]

	async def didScanLineFlying(
		tool,
		*,
		axis,
		positions,
		fixed_mm,
		rpm,
		progress_label,
		should_abort=None,
		**kwargs,
	):
		return list(profile), FlyingScanLine(
			axis=axis,
			fixed_mm=fixed_mm,
			start_mm=float(positions[0]),
			end_mm=float(positions[-1]),
			rpm=rpm,
			samples=samples,
			duration_s=1.0,
		)

	monkeypatch.setattr(flying_module, "scan_line_positions_flying", didScanLineFlying)

	flying_scan = GridScanResult(
		x_positions=positions,
		y_positions=[10.0],
		depth_rows=[[None for _ in positions]],
	)
	segment = await fine_scan_module.probe_line_from_center(
		SimpleNamespace(_mx=SimpleNamespace(_params=SimpleNamespace(mpr=80.0))),
		base_scan=flying_scan,
		axis="x",
		positions=positions,
		fixed_mm=10.0,
		center_idx=center_idx,
		step_mm=step_mm,
		progress_prefix="inferred_far_edge",
		settle_ms=0,
		samples_per_point=1,
		wall_depth_mm=9.5,
		expected_span_mm=26.0,
		should_infer_far_edge=True,
		scan_config=SimpleNamespace(fine_scan_style="flying", fine_scan_feed_mm_s=40.0),
	)

	# The far edge was inferred from the expected span, so refining the near edge has to move
	# it too. Keeping the pre-refinement end would give 25.75mm and centre 32.125mm.
	assert segment is not None
	assert segment.start_mm == pytest.approx(refined_near_edge_mm)
	assert segment.width_mm == pytest.approx(26.0)
	assert segment.end_mm == pytest.approx(refined_near_edge_mm + 26.0)
	assert segment.center_mm == pytest.approx(refined_near_edge_mm + 13.0)


def test_refine_segment_edges_from_samples_uses_interpolated_crossings() -> None:
	from openshelf_os.laser_calibration import LineGapSegment, refine_segment_edges_from_samples

	positions = [18.0, 20.0, 22.0, 40.0, 42.0, 44.0]
	depth_values = [9.5, 0.0, 0.0, 0.0, 0.0, 9.5]
	segment = LineGapSegment(
		start_idx=1,
		end_idx=4,
		start_mm=19.0,
		end_mm=43.0,
		width_mm=24.0,
		center_mm=31.0,
	)
	samples = []

	for index in range(0, 107):
		position_mm = 0.5 * index
		depth_mm = 9.5 if position_mm < 19.25 or position_mm > 42.6 else 0.0
		samples.append(
			FlyingScanSample(position_mm=position_mm, depth_mm=depth_mm, elapsed_s=index * 0.01)
		)

	refined = refine_segment_edges_from_samples(
		segment=segment,
		positions=positions,
		depth_values=depth_values,
		samples=samples,
	)

	assert refined.start_idx == segment.start_idx
	assert refined.end_idx == segment.end_idx
	assert refined.start_mm == pytest.approx(19.25)
	assert refined.end_mm == pytest.approx(42.75)
	assert refined.width_mm == pytest.approx(42.75 - 19.25)
	assert refined.center_mm == pytest.approx((19.25 + 42.75) / 2.0)


def test_refine_segment_edges_keeps_grid_edges_without_crossings() -> None:
	from openshelf_os.laser_calibration import LineGapSegment, refine_segment_edges_from_samples

	positions = [18.0, 20.0, 22.0, 40.0, 42.0, 44.0]
	depth_values = [9.5, 0.0, 0.0, 0.0, 0.0, 9.5]
	segment = LineGapSegment(
		start_idx=1,
		end_idx=4,
		start_mm=19.0,
		end_mm=43.0,
		width_mm=24.0,
		center_mm=31.0,
	)
	samples = [
		FlyingScanSample(position_mm=20.0, depth_mm=0.0, elapsed_s=0.0),
		FlyingScanSample(position_mm=40.0, depth_mm=0.0, elapsed_s=1.0),
	]

	refined = refine_segment_edges_from_samples(
		segment=segment,
		positions=positions,
		depth_values=depth_values,
		samples=samples,
	)

	assert refined.start_mm == pytest.approx(segment.start_mm)
	assert refined.end_mm == pytest.approx(segment.end_mm)
	assert refined.center_mm == pytest.approx(segment.center_mm)


@pytest.mark.asyncio
async def test_find_hole_interior_idx_from_seed_flying_matches_stepped(monkeypatch) -> None:
	import openshelf_os.laser_calibration.flying as flying_module
	import openshelf_os.laser_calibration.shelf_scan as shelf_scan_module

	positions = [float(value) for value in range(0, 20, 2)]
	profile = [0.0 if 5 <= index <= 9 else 9.5 for index in range(len(positions))]
	stepped_scan = GridScanResult(
		x_positions=[10.0],
		y_positions=positions,
		depth_rows=[[None] for _ in positions],
	)

	async def didSampleStepped(tool, *, x_mm, y_mm, settle_ms, samples_per_point, should_abort=None):
		return profile[positions.index(y_mm)]

	monkeypatch.setattr(shelf_scan_module, "sample_scan_point", didSampleStepped)

	stepped_idx = await shelf_scan_module.find_hole_interior_idx_from_seed(
		SimpleNamespace(),
		base_scan=stepped_scan,
		axis="y",
		positions=positions,
		fixed_mm=10.0,
		start_idx=2,
		progress_prefix="stepped_seek",
		settle_ms=0,
		samples_per_point=1,
		wall_depth_mm=9.5,
	)

	async def didFailToSample(*args, **kwargs):
		raise AssertionError("stepped sampling used while flying")

	monkeypatch.setattr(shelf_scan_module, "sample_scan_point", didFailToSample)

	async def didScanLineFlying(
		tool,
		*,
		axis,
		positions,
		fixed_mm,
		rpm,
		progress_label,
		should_abort=None,
		**kwargs,
	):
		return list(profile), FlyingScanLine(
			axis=axis,
			fixed_mm=fixed_mm,
			start_mm=float(positions[0]),
			end_mm=float(positions[-1]),
			rpm=rpm,
			samples=[
				FlyingScanSample(position_mm=position_mm, depth_mm=depth_mm, elapsed_s=index * 0.02)
				for index, (position_mm, depth_mm) in enumerate(zip(positions, profile))
			],
			duration_s=1.0,
		)

	monkeypatch.setattr(flying_module, "scan_line_positions_flying", didScanLineFlying)

	flying_scan = GridScanResult(
		x_positions=[10.0],
		y_positions=positions,
		depth_rows=[[None] for _ in positions],
	)
	recorded_lines: list[str] = []
	flying_idx = await shelf_scan_module.find_hole_interior_idx_from_seed(
		SimpleNamespace(_my=SimpleNamespace(_params=SimpleNamespace(mpr=40.0))),
		base_scan=flying_scan,
		axis="y",
		positions=positions,
		fixed_mm=10.0,
		start_idx=2,
		progress_prefix="flying_seek",
		settle_ms=0,
		samples_per_point=1,
		wall_depth_mm=9.5,
		scan_config=SimpleNamespace(fine_scan_style="flying", fine_scan_feed_mm_s=40.0),
		on_flying_line=lambda label, line: recorded_lines.append(label),
	)

	assert stepped_idx == 5
	assert flying_idx == stepped_idx
	assert recorded_lines == ["flying_seek_flying"]


@pytest.mark.asyncio
async def test_find_hole_interior_idx_from_seed_resamples_uncovered_flying_bins(monkeypatch) -> None:
	positions = [float(value) for value in range(0, 20, 2)]
	profile = [0.0 if 5 <= index <= 9 else 9.5 for index in range(len(positions))]
	# The flying pass left the whole opening unobserved.
	flying_profile = [None if 5 <= index <= 9 else depth for index, depth in enumerate(profile)]
	resampled_y: list[float] = []

	async def didSampleStepped(tool, *, x_mm, y_mm, settle_ms, samples_per_point, should_abort=None):
		resampled_y.append(y_mm)
		return profile[positions.index(y_mm)]

	monkeypatch.setattr(shelf_scan_module, "sample_scan_point", didSampleStepped)

	async def didScanLineFlying(tool, *, axis, positions, fixed_mm, rpm, progress_label, **kwargs):
		return list(flying_profile), FlyingScanLine(
			axis=axis,
			fixed_mm=fixed_mm,
			start_mm=float(positions[0]),
			end_mm=float(positions[-1]),
			rpm=rpm,
			samples=[],
			duration_s=1.0,
		)

	monkeypatch.setattr(flying_module, "scan_line_positions_flying", didScanLineFlying)

	flying_scan = GridScanResult(
		x_positions=[10.0],
		y_positions=positions,
		depth_rows=[[None] for _ in positions],
	)
	flying_idx = await shelf_scan_module.find_hole_interior_idx_from_seed(
		SimpleNamespace(_my=SimpleNamespace(_params=SimpleNamespace(mpr=40.0))),
		base_scan=flying_scan,
		axis="y",
		positions=positions,
		fixed_mm=10.0,
		start_idx=2,
		progress_prefix="flying_seek",
		settle_ms=0,
		samples_per_point=1,
		wall_depth_mm=9.5,
		scan_config=SimpleNamespace(fine_scan_style="flying", fine_scan_feed_mm_s=40.0),
	)

	assert flying_idx == 5
	assert resampled_y == [positions[5]]


def test_flying_scan_line_metrics_use_travelled_distance() -> None:
	line = FlyingScanLine(
		axis="y",
		fixed_mm=0.0,
		start_mm=1250.0,
		end_mm=0.0,
		rpm=30,
		duration_s=5.0,
		samples=[
			FlyingScanSample(position_mm=1165.0, depth_mm=None, elapsed_s=0.0),
			FlyingScanSample(
				position_mm=1065.0,
				depth_mm=9.5,
				elapsed_s=4.9,
				depth_position_mm=1065.0,
			),
		],
	)

	# 100mm travelled over 5s, not the commanded 1250mm or a start clamped to the axis limit.
	assert "feed_mm_s=20.00" in line.metrics_text()


def test_build_shelf_edge_stop_predicate_needs_a_confirmed_run() -> None:
	from openshelf_os.laser_calibration.shelf_scout import build_shelf_edge_stop_predicate

	def sample(position_mm, depth_mm):
		return FlyingScanSample(position_mm=position_mm, depth_mm=depth_mm, elapsed_s=0.0)

	# confirmation is distance-based: the run must span min_positive_run_length grid steps
	# (3 * 2mm = 6mm), so three 2mm-spaced positives covering 4mm must not confirm
	predicate = build_shelf_edge_stop_predicate(min_positive_run_length=3)
	assert predicate([sample(0.0, None)]) is False
	assert predicate([sample(0.0, None), sample(2.0, 9.5)]) is False
	assert predicate([sample(0.0, None), sample(2.0, 9.5), sample(4.0, 9.5)]) is False
	assert predicate([
		sample(0.0, None), sample(2.0, 9.5), sample(4.0, 9.5), sample(6.0, 9.5),
	]) is False
	assert predicate([
		sample(0.0, None), sample(2.0, 9.5), sample(4.0, 9.5), sample(6.0, 9.5), sample(8.0, 0.0),
	]) is False

	# a run spanning the full 6mm confirms on the first non-positive sample after it
	confirmed = build_shelf_edge_stop_predicate(min_positive_run_length=3)
	assert confirmed([sample(2.0, 9.5)]) is False
	assert confirmed([sample(2.0, 9.5), sample(4.0, 9.5)]) is False
	assert confirmed([sample(2.0, 9.5), sample(4.0, 9.5), sample(6.0, 9.5)]) is False
	assert confirmed([
		sample(2.0, 9.5), sample(4.0, 9.5), sample(6.0, 9.5), sample(8.0, 9.5),
	]) is False
	assert confirmed([
		sample(2.0, 9.5), sample(4.0, 9.5), sample(6.0, 9.5), sample(8.0, 9.5), sample(10.0, 0.0),
	]) is True

	# an unconfirmed run resets, so a later short run cannot confirm either
	short_run = build_shelf_edge_stop_predicate(min_positive_run_length=3)
	assert short_run([sample(0.0, 9.5)]) is False
	assert short_run([sample(0.0, 9.5), sample(2.0, 0.0)]) is False
	assert short_run([sample(0.0, 9.5), sample(2.0, 0.0), sample(4.0, 9.5)]) is False
	assert short_run([
		sample(0.0, 9.5), sample(2.0, 0.0), sample(4.0, 9.5), sample(6.0, 9.5),
	]) is False
	assert short_run([
		sample(0.0, 9.5), sample(2.0, 0.0), sample(4.0, 9.5), sample(6.0, 9.5), sample(8.0, 0.0),
	]) is False


def test_shelf_edge_stop_predicate_matches_the_detector_on_dense_samples() -> None:
	from openshelf_os.laser_calibration.shelf_scout import build_shelf_edge_stop_predicate

	grid = [0.0, 2.0, 4.0, 6.0, 8.0]

	# a flying pass samples far finer than the 2mm scout grid; three 1mm-spaced positives span
	# only 2mm of travel, so the predicate must not stop and the binned detector rejects the run
	short_samples = [
		FlyingScanSample(position_mm=float(index), depth_mm=depth_mm, elapsed_s=index * 0.02)
		for index, depth_mm in enumerate([0.5, 0.5, 0.5, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0])
	]
	short_predicate = build_shelf_edge_stop_predicate(min_positive_run_length=3)
	short_stops = [
		short_predicate(short_samples[:count]) for count in range(1, len(short_samples) + 1)
	]
	assert short_stops == [False] * len(short_samples)

	short_binned = bin_samples_to_positions(samples=short_samples, positions=grid)
	assert detect_shelf_edges_from_line(x_mm=10.0, y_positions=grid, depth_values=short_binned) == []

	# once the positive travel reaches min_positive_run_length grid steps the predicate stops
	# the pass, and the binned detector confirms the same edge
	long_samples = [
		FlyingScanSample(position_mm=float(index), depth_mm=depth_mm, elapsed_s=index * 0.02)
		for index, depth_mm in enumerate([0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, -1.0, -1.0, -1.0])
	]
	long_predicate = build_shelf_edge_stop_predicate(min_positive_run_length=3)
	long_stops = [
		long_predicate(long_samples[:count]) for count in range(1, len(long_samples) + 1)
	]
	assert long_stops == [False] * 7 + [True, False, False]

	long_binned = bin_samples_to_positions(samples=long_samples, positions=grid)
	assert len(detect_shelf_edges_from_line(x_mm=10.0, y_positions=grid, depth_values=long_binned)) == 1


@pytest.mark.asyncio
async def test_scan_axis_flying_stops_early_when_asked() -> None:
	tool = FakeFlyingTool(depth_mm=9.5)
	seen_samples: list[list] = []

	def didRequestStop(samples: list) -> bool:
		seen_samples.append(list(samples))
		return len(samples) >= 3

	line = await scan_axis_flying(
		tool=tool,
		axis="y",
		start_mm=0.0,
		end_mm=100.0,
		fixed_mm=0.0,
		rpm=30,
		progress_label="flying_stop_when",
		stop_when=didRequestStop,
	)

	positions = [sample.position_mm for sample in line.samples]

	assert len(seen_samples) > 0
	assert positions[-1] < 100.0
	assert len(line.samples) < 10
	assert tool._my.is_moving is False
	assert tool._my.stop_count == 1
	# Only the pre-position wait: a line stopped by its own predicate is not waited on.
	assert tool.gantry_wait_calls == 1


@pytest.mark.asyncio
async def test_scan_axis_flying_skips_the_stationary_end_sample_when_stopped_early() -> None:
	tool = FakeFlyingTool(depth_mm=5.0)

	def didRequestStop(samples: list) -> bool:
		return len(samples) >= 3

	line = await scan_axis_flying(
		tool=tool,
		axis="y",
		start_mm=0.0,
		end_mm=100.0,
		fixed_mm=0.0,
		rpm=30,
		progress_label="flying_stop_when_no_end_sample",
		stop_when=didRequestStop,
	)

	# The stationary end sample belongs to the settled normal path: a line that stopped itself
	# returns without it, so the reads and the last sample reflect the stop, not the end.
	assert len(line.samples) == 3
	assert tool.depth_reads == 3
	assert line.samples[-1].position_mm == pytest.approx(2.0)
	assert line.samples[-1].velocity_mm_s != 0.0


@pytest.mark.asyncio
async def test_angle_shelf_scout_uses_the_flying_line_when_configured(monkeypatch) -> None:
	import openshelf_os.laser_calibration.angle.shelf_scout as angle_scout_module

	y_positions = [100.0, 98.0, 96.0]
	flying_requests: list[dict] = []

	async def didScanFlying(tool, **kwargs):
		flying_requests.append(kwargs)
		return (
			[9.5, 9.5, None],
			[ShelfSurfaceEdge(x_mm=25.0, y_mm=93.0)],
			FlyingScanLine(
				axis="y",
				fixed_mm=25.0,
				start_mm=100.0,
				end_mm=96.0,
				rpm=30,
				samples=[],
				duration_s=1.0,
			),
		)

	async def didFailToStep(*args, **kwargs):
		raise AssertionError("stepped scout used while flying")

	monkeypatch.setattr(angle_scout_module, "scan_shelf_scout_line_flying", didScanFlying)
	monkeypatch.setattr(angle_scout_module, "scan_shared_shelf_scout_line", didFailToStep)

	depth_values, edges = await angle_scout_module.scan_shelf_scout_line(
		SimpleNamespace(_my=SimpleNamespace(_params=SimpleNamespace(mpr=40.0))),
		x_mm=25.0,
		y_positions=y_positions,
		settle_ms=0,
		samples_per_point=1,
		progress_label="shelf_scout_1",
		stop_after_first_edge=True,
		scan_config=SimpleNamespace(coarse_scan_style="flying", coarse_scan_rpm=30),
	)

	assert depth_values == [9.5, 9.5, None]
	assert edges == [ShelfSurfaceEdge(x_mm=25.0, y_mm=93.0)]
	assert len(flying_requests) == 1
	assert flying_requests[0]["rpm"] == 30
	assert flying_requests[0]["stop_after_first_edge"] is True
	assert flying_requests[0]["stop_when"] is not None

	# The streaming predicate confirms once the positive run spans the distance the post-pass
	# detector needs (min_positive_run_length grid steps), never on sample count alone.
	predicate = flying_requests[0]["stop_when"]
	assert predicate([FlyingScanSample(position_mm=100.0, depth_mm=9.5, elapsed_s=0.0)]) is False
	assert predicate([
		FlyingScanSample(position_mm=100.0, depth_mm=9.5, elapsed_s=0.0),
		FlyingScanSample(position_mm=98.0, depth_mm=9.5, elapsed_s=0.1),
	]) is False
	assert predicate([
		FlyingScanSample(position_mm=100.0, depth_mm=9.5, elapsed_s=0.0),
		FlyingScanSample(position_mm=98.0, depth_mm=9.5, elapsed_s=0.1),
		FlyingScanSample(position_mm=96.0, depth_mm=9.5, elapsed_s=0.2),
	]) is False
	assert predicate([
		FlyingScanSample(position_mm=100.0, depth_mm=9.5, elapsed_s=0.0),
		FlyingScanSample(position_mm=98.0, depth_mm=9.5, elapsed_s=0.1),
		FlyingScanSample(position_mm=96.0, depth_mm=9.5, elapsed_s=0.2),
		FlyingScanSample(position_mm=94.0, depth_mm=9.5, elapsed_s=0.3),
	]) is False
	assert predicate([
		FlyingScanSample(position_mm=100.0, depth_mm=9.5, elapsed_s=0.0),
		FlyingScanSample(position_mm=98.0, depth_mm=9.5, elapsed_s=0.1),
		FlyingScanSample(position_mm=96.0, depth_mm=9.5, elapsed_s=0.2),
		FlyingScanSample(position_mm=94.0, depth_mm=9.5, elapsed_s=0.3),
		FlyingScanSample(position_mm=92.0, depth_mm=0.0, elapsed_s=0.4),
	]) is True


@pytest.mark.asyncio
async def test_estimate_seeded_candidate_wall_depths_flies_when_configured(monkeypatch) -> None:
	import openshelf_os.laser_calibration.staged_scan as staged_scan_module

	bounds = ScanBounds(x_min=0.0, x_max=60.0, y_min=0.0, y_max=100.0)
	x_positions = build_axis_positions(0.0, 60.0, 4.0)
	depth_values = [9.5 if position <= 16.0 or position >= 48.0 else 0.0 for position in x_positions]
	recorded_lines: list[str] = []
	flying_requests: list[dict] = []

	async def didScanFlying(tool, **kwargs):
		flying_requests.append(kwargs)
		return (
			list(depth_values),
			FlyingScanLine(
				axis="x",
				fixed_mm=50.0,
				start_mm=0.0,
				end_mm=60.0,
				rpm=30,
				samples=[],
				duration_s=1.0,
			),
		)

	async def didFailToStep(*args, **kwargs):
		raise AssertionError("stepped wall-depth line used while flying")

	monkeypatch.setattr(staged_scan_module, "scan_line_positions_flying", didScanFlying)
	monkeypatch.setattr(staged_scan_module, "scan_line_positions", didFailToStep)

	candidate = GapCandidateRegion(
		x_min=18.0,
		x_max=46.0,
		y_min=44.0,
		y_max=56.0,
		wall_depth_mm=None,
	)
	coarse_scan = GridScanResult(x_positions=[], y_positions=[], depth_rows=[])

	await staged_scan_module.estimate_seeded_candidate_wall_depths(
		SimpleNamespace(),
		bounds=bounds,
		candidates=[candidate],
		settle_ms=0,
		samples_per_point=1,
		scan_config=SimpleNamespace(
			coarse_scan_style="flying",
			coarse_scan_rpm=30,
			coarse_scan_bin_mode="nearest",
		),
		coarse_scan=coarse_scan,
		on_flying_line=lambda label, line: recorded_lines.append(label),
	)

	assert candidate.wall_depth_mm == pytest.approx(9.5)
	assert candidate.wall_sample_count >= 1
	assert flying_requests[0]["rpm"] == 30
	assert recorded_lines == ["seeded_wall_depth_1_flying"]
	assert any(
		value is not None for row in coarse_scan.depth_rows for value in row
	)


async def _scan_scout_line_with_flying_bins(
	monkeypatch,
	*,
	y_positions: list[float],
	binned_depths: list[float | None],
	profile: dict[float, float],
	post_edge_skip_mm: float = 40.0,
) -> tuple[list[float | None], list[ShelfSurfaceEdge], list[float]]:
	resample_requests: list[float] = []

	async def didScanLineFlying(tool, *, axis, positions, fixed_mm, rpm, progress_label, **kwargs):
		return list(binned_depths), FlyingScanLine(
			axis=axis,
			fixed_mm=fixed_mm,
			start_mm=float(positions[0]),
			end_mm=float(positions[-1]),
			rpm=rpm,
			samples=[],
			duration_s=1.0,
		)

	monkeypatch.setattr(flying_module, "scan_line_positions_flying", didScanLineFlying)
	tool = FakeFlyingTool()

	async def didSampleStepped(tool, *, settle_ms, samples_per_point):
		y_mm = tool._my.position_mm
		resample_requests.append(y_mm)
		return profile[y_mm]

	depth_values, edges, _ = await flying_module.scan_shelf_scout_line_flying(
		tool,
		x_mm=25.0,
		y_positions=y_positions,
		rpm=30,
		progress_label="shelf_scout_resample",
		post_edge_skip_mm=post_edge_skip_mm,
		sample_depth=didSampleStepped,
		progress_printer=lambda message: None,
	)

	return depth_values, edges, resample_requests


@pytest.mark.asyncio
async def test_flying_shelf_scout_resamples_an_uncovered_bin_instead_of_reading_an_edge(
	monkeypatch,
) -> None:
	y_positions = [float(y_mm) for y_mm in range(200, 98, -2)]
	profile = {y_mm: 9.5 for y_mm in y_positions}
	binned_depths = [None if y_mm == 192.0 else 9.5 for y_mm in y_positions]

	depth_values, edges, resample_requests = await _scan_scout_line_with_flying_bins(
		monkeypatch,
		y_positions=y_positions,
		binned_depths=binned_depths,
		profile=profile,
	)

	# A constant brim never closes its run; the empty bin after three positives is probed
	# instead of read as the gap that would confirm an edge.
	assert edges == []
	assert resample_requests == [192.0]
	assert depth_values[y_positions.index(192.0)] == 9.5


@pytest.mark.asyncio
async def test_flying_shelf_scout_leaves_skipped_bins_unprobed(monkeypatch) -> None:
	y_positions = [float(y_mm) for y_mm in range(200, 98, -2)]
	profile = {y_mm: (9.5 if y_mm > 170.0 else 0.0) for y_mm in y_positions}
	uncovered_y_mm = {160.0, 120.0}
	binned_depths = [None if y_mm in uncovered_y_mm else profile[y_mm] for y_mm in y_positions]

	depth_values, edges, resample_requests = await _scan_scout_line_with_flying_bins(
		monkeypatch,
		y_positions=y_positions,
		binned_depths=binned_depths,
		profile=profile,
	)

	# 160mm sits inside the post-edge skip after the 170mm edge, so the stepped walk would
	# never read it; only 120mm is probed.
	assert edges == [ShelfSurfaceEdge(x_mm=25.0, y_mm=163.0)]
	assert resample_requests == [120.0]
	assert depth_values[y_positions.index(160.0)] is None
	assert depth_values[y_positions.index(120.0)] == 0.0
