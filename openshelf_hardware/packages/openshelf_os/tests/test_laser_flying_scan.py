import argparse
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openshelf_os.laser_calibration import (
	FlyingScanLine,
	FlyingScanSample,
	ShelfSurfaceEdge,
	StorageModuleSide,
	GridScanResult,
	scan_axis_flying,
	scan_line_positions_flying,
)
from openshelf_os.config.laser_scan import LaserScanConfig, LaserScanXYOffsetConfig
from openshelf_os.laser_calibration.core import get_flying_capture_latency_ms
from openshelf_os.laser_calibration.defaults import FLYING_CAPTURE_LATENCY_MS
from openshelf_os.laser_calibration.recording import build_run_parameters
from openshelf_os.laser_calibration.scan import ScanBounds, build_axis_positions
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


def test_estimate_axis_ramp_mm_derives_the_ramp_from_accel_and_rpm() -> None:
	# 40mm/rev at 60rpm is 40mm/s; 500ms per 1000rpm ramps to 60rpm in 30ms, covering 0.6mm.
	motor = SimpleNamespace(_params=SimpleNamespace(mpr=40.0, accel=500))

	assert flying_module.estimate_axis_ramp_mm(motor, 60) == pytest.approx(0.6)
	assert flying_module.estimate_axis_ramp_mm(SimpleNamespace(_params=SimpleNamespace(mpr=40.0)), 60) is None


@pytest.mark.asyncio
async def test_scan_axis_flying_starts_before_the_grid_and_arms_the_stop_after_the_lead_in() -> None:
	tool = FakeFlyingTool(depth_mm=5.0)
	tool._my = FakeTimedAxisMotor(feed_mm_s=40.0)
	tool._my._params = SimpleNamespace(mpr=40.0, accel=500, lower_lim=0.0, lim=100.0)
	lead_in_mm = 0.6 + flying_module.FLYING_SCAN_LEAD_IN_MARGIN_MM
	seen_positions: list[float] = []

	def stop_when(samples: list[FlyingScanSample]) -> bool:
		seen_positions.append(flying_module.sample_depth_position_mm(samples[-1]))
		return False

	line = await scan_axis_flying(
		tool=tool,
		axis="y",
		start_mm=20.0,
		end_mm=30.0,
		fixed_mm=25.0,
		rpm=60,
		progress_label="flying_lead_in",
		capture_latency_s=0.0,
		stop_when=stop_when,
	)

	assert tool._my.moves[0] == (pytest.approx(20.0 - lead_in_mm), None)
	# No stationary sample is taken at the lead-in start; the first sample is a moving one.
	assert line.samples[0].position_mm > 20.0 - lead_in_mm
	assert len(seen_positions) > 0
	assert all(position_mm >= 20.0 for position_mm in seen_positions)


@pytest.mark.asyncio
async def test_scan_axis_flying_keeps_the_stationary_start_when_the_limit_clamps_the_lead_in() -> None:
	tool = FakeFlyingTool(depth_mm=5.0)
	tool._my = FakeTimedAxisMotor(feed_mm_s=40.0)
	tool._my._params = SimpleNamespace(mpr=40.0, accel=500, lower_lim=0.0, lim=100.0)

	line = await scan_axis_flying(
		tool=tool,
		axis="y",
		start_mm=0.0,
		end_mm=10.0,
		fixed_mm=25.0,
		rpm=60,
		progress_label="flying_clamped_lead_in",
		capture_latency_s=0.0,
	)

	assert tool._my.moves[0] == (pytest.approx(0.0), None)
	assert line.samples[0].position_mm == pytest.approx(0.0)
	assert line.samples[0].velocity_mm_s is None


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


def test_flying_capture_latency_falls_back_to_the_default_and_rejects_negative_values() -> None:
	assert get_flying_capture_latency_ms(LaserScanConfig()) == FLYING_CAPTURE_LATENCY_MS
	assert get_flying_capture_latency_ms(LaserScanConfig(flying_capture_latency_ms=37.5)) == 37.5
	assert get_flying_capture_latency_ms(LaserScanConfig(flying_capture_latency_ms=0.0)) == 0.0

	with pytest.raises(ValueError, match="flying_capture_latency_ms"):
		get_flying_capture_latency_ms(SimpleNamespace(flying_capture_latency_ms=-1.0))


@pytest.mark.asyncio
async def test_prefetch_line_depth_values_corrects_with_the_configured_capture_latency(
	monkeypatch,
) -> None:
	captured_latencies: list[float | None] = []

	async def didScanLineFlying(tool, *, axis, positions, fixed_mm, rpm, progress_label, **kwargs):
		captured_latencies.append(kwargs.get("capture_latency_s"))
		return [None for _ in positions], FlyingScanLine(
			axis=axis, fixed_mm=fixed_mm, start_mm=positions[0], end_mm=positions[-1], rpm=rpm
		)

	monkeypatch.setattr(flying_module, "scan_line_positions_flying", didScanLineFlying)

	await flying_module.prefetch_line_depth_values(
		SimpleNamespace(_mx=SimpleNamespace(_params=SimpleNamespace(mpr=80.0))),
		scan_config=SimpleNamespace(
			fine_scan_style="flying",
			fine_scan_feed_mm_s=40.0,
			flying_capture_latency_ms=37.5,
		),
		axis="x",
		positions=[0.0, 1.0, 2.0],
		fixed_mm=10.0,
		progress_label="latency_prefetch",
	)

	assert captured_latencies == [pytest.approx(0.0375)]


@pytest.mark.asyncio
async def test_flying_line_samples_record_the_capture_latency_they_were_corrected_with(
	tmp_path: Path,
) -> None:
	line = await scan_axis_flying(
		tool=FakeFlyingTool(depth_mm=5.0),
		axis="y",
		start_mm=0.0,
		end_mm=10.0,
		fixed_mm=25.0,
		rpm=30,
		progress_label="flying_latency_record",
		capture_latency_s=0.0375,
	)
	output_path = flying_module.write_flying_line_samples(
		run_dir=tmp_path,
		label="flying_latency_record",
		line=line,
	)

	assert line.capture_latency_s == pytest.approx(0.0375)
	rows = [json.loads(row) for row in Path(output_path).read_text(encoding="utf-8").splitlines()]
	assert len(rows) > 0
	assert all(row["capture_latency_s"] == pytest.approx(0.0375) for row in rows)


def test_run_parameters_record_the_resolved_capture_latency() -> None:
	scan_config = LaserScanConfig(
		laser_offset_x_mm=0.0,
		laser_offset_y_mm=0.0,
		front_gripper_xy_correction_mm=LaserScanXYOffsetConfig(x_mm=0.0, y_mm=0.0),
		flying_capture_latency_ms=37.5,
	)
	args = argparse.Namespace(
		module="front",
		x=None,
		y=None,
		y_offset=0.0,
		settle_ms=None,
		samples_per_point=None,
		known_shelf_edge_y=None,
		npy=None,
		scans_dir="scans",
		config="config.json",
		apply=False,
		shelf_scout_only=False,
		rescan_cell=None,
		flying_capture_latency_ms=None,
	)

	run_parameters = build_run_parameters(
		args=args,
		side=StorageModuleSide.FRONT,
		bounds=ScanBounds(x_min=0.0, x_max=100.0, y_min=0.0, y_max=100.0),
		step_mm=1.0,
		settle_ms=0,
		samples_per_point=1,
		coarse_settle_ms=0,
		coarse_samples_per_point=1,
		scan_config=scan_config,
	)

	assert run_parameters["resolved"]["flying_capture_latency_ms"] == 37.5
	assert run_parameters["cli"]["flying_capture_latency_ms"] is None
