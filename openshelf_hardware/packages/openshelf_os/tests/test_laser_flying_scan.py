import asyncio
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
	ShelfSurfaceEdge,
	StorageModuleSide,
	bin_samples_to_positions,
	detect_shelf_edges_from_line,
	estimate_axis_feed_mm_s,
	estimate_sample_velocity_mm_s,
	find_depth_threshold_crossings,
	get_coarse_scan_bin_mode,
	get_coarse_scan_rpm,
	get_coarse_scan_style,
	sample_depth_position_mm,
	scan_axis_flying,
	scan_line_positions_flying,
	scan_shelf_scout_line,
)
from openshelf_os.laser_calibration.scan import ScanBounds, build_axis_positions
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
		side=StorageModuleSide.LEFT,
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
		side=StorageModuleSide.LEFT,
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
