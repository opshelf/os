from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import sys
import time
import tempfile
from pathlib import Path
from typing import Any, Callable

import numpy as np

LASER_SCAN_STAGE_EXTENSION_MM = 13.0
LASER_SCAN_Z_OFFSET_MM = 10.0
SHELF_SCOUT_STEP_MM = 2.0
SHELF_SCOUT_X_OFFSETS_MM = [0.0]
LEFT_MODULE_ROTATION_CENTER_DEG = 0.0
RIGHT_MODULE_ROTATION_CENTER_DEG = 180.0
RIGHT_MODULE_SHELF_SCOUT_X_MM = 25.0
RIGHT_MODULE_ROTATION_SAFE_X_MM = 475.0
SHELF_EDGE_CONFIRMATION_COUNT = 3
SHELF_BRIM_MIN_HEIGHT_MM = 8.0
SHELF_BRIM_MAX_HEIGHT_MM = 14.0
SHELF_MIN_SPACING_MM = 40.0
SHELF_EDGE_TOP_MARGIN_MM = 5.0
SHELF_SCOUT_POST_EDGE_SKIP_MM = 80.0
GAP_DEPTH_THRESHOLD_MM = 0.01


@dataclass
class LaserAngleCalibrationSample:
	axis_x_mm: float
	axis_z_mm: float
	rotation_deg: float
	absolute_rotation_deg: float
	stage_extension_mm: float
	z_extension_mm: float
	distance_mm: float


@dataclass
class FittedLine:
	point_x_mm: float
	point_z_mm: float
	direction_x: float
	direction_z: float
	normal_x: float
	normal_z: float
	line_angle_deg: float
	rms_residual_mm: float
	max_abs_residual_mm: float


@dataclass
class AlphaFitResult:
	alpha_deg: float
	sample_count: int
	line: FittedLine


@dataclass
class ShelfSurfaceEdge:
	x_mm: float
	y_mm: float


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


def get_current_timestamp() -> str:
	return datetime.now(timezone.utc).isoformat()


def should_skip_motion_cleanup_on_cancel() -> bool:
	_, active_exception, _ = sys.exc_info()

	if isinstance(active_exception, asyncio.CancelledError) is True:
		return True

	current_task = asyncio.current_task()

	if current_task is None:
		return False

	return current_task.cancelling() > 0


def raise_if_cancelled(should_abort: Callable[[], bool] | None = None) -> None:
	if should_abort is not None and should_abort() is True:
		raise asyncio.CancelledError()

	current_task = asyncio.current_task()

	if current_task is not None and current_task.cancelling() > 0:
		raise asyncio.CancelledError()


def print_status(message: str) -> None:
	print(message, flush=True)


def depth_to_label(depth_mm: float | None) -> str:
	if depth_mm is None:
		return 'none'

	return f'{depth_mm:.2f}'


def is_positive_depth(depth_mm: float | None) -> bool:
	if depth_mm is None:
		return False

	return depth_mm > GAP_DEPTH_THRESHOLD_MM


def build_axis_positions(start_mm: float, end_mm: float, step_mm: float) -> list[float]:
	if step_mm <= 0:
		raise ValueError('step_mm must be positive')

	positions: list[float] = []
	direction = 1.0 if end_mm >= start_mm else -1.0
	current_mm = start_mm

	while True:
		rounded_position_mm = round(current_mm, 4)

		if len(positions) == 0 or positions[-1] != rounded_position_mm:
			positions.append(rounded_position_mm)

		if direction > 0 and current_mm >= end_mm:
			break

		if direction < 0 and current_mm <= end_mm:
			break

		current_mm += step_mm * direction

		if direction > 0 and current_mm > end_mm:
			current_mm = end_mm

		if direction < 0 and current_mm < end_mm:
			current_mm = end_mm

	return positions


def build_rotation_matrix(angle_rad: float) -> np.ndarray:
	cos_angle = math.cos(angle_rad)
	sin_angle = math.sin(angle_rad)

	return np.array(
		[
			[cos_angle, -sin_angle],
			[sin_angle, cos_angle],
		],
		dtype=np.float64,
	)


def build_line_direction(direction_angle_deg: float) -> np.ndarray:
	direction_angle_rad = math.radians(direction_angle_deg)

	return np.array(
		[
			math.cos(direction_angle_rad),
			math.sin(direction_angle_rad),
		],
		dtype=np.float64,
	)


def build_line_normal(direction: np.ndarray) -> np.ndarray:
	normal = np.array([-direction[1], direction[0]], dtype=np.float64)
	norm = float(np.linalg.norm(normal))

	if norm == 0.0:
		raise ValueError('line direction must be non-zero')

	return normal / norm


def reconstruct_hit_points(
	samples: list[LaserAngleCalibrationSample],
	*,
	alpha_deg: float,
	laser_offset_x_mm: float,
	laser_offset_z_mm: float,
	beam_zero_deg: float,
) -> np.ndarray:
	points: list[np.ndarray] = []
	local_offset = np.array([laser_offset_x_mm, laser_offset_z_mm], dtype=np.float64)
	beam_zero_rad = math.radians(beam_zero_deg)

	for sample in samples:
		sample_laser_offset_x_mm = laser_offset_x_mm + sample.stage_extension_mm
		sample_laser_offset_z_mm = laser_offset_z_mm + sample.z_extension_mm
		local_offset = np.array(
			[sample_laser_offset_x_mm, sample_laser_offset_z_mm],
			dtype=np.float64,
		)
		effective_angle_rad = math.radians(sample.absolute_rotation_deg + alpha_deg)
		rotation_matrix = build_rotation_matrix(effective_angle_rad)
		axis_point = np.array([sample.axis_x_mm, sample.axis_z_mm], dtype=np.float64)
		emitter_point = axis_point + (rotation_matrix @ local_offset)
		beam_direction = np.array(
			[
				math.cos(effective_angle_rad + beam_zero_rad),
				math.sin(effective_angle_rad + beam_zero_rad),
			],
			dtype=np.float64,
		)
		hit_point = emitter_point + (beam_direction * sample.distance_mm)
		points.append(hit_point)

	if len(points) == 0:
		return np.empty((0, 2), dtype=np.float64)

	return np.vstack(points)


def fit_line(points: np.ndarray) -> FittedLine:
	if len(points) < 2:
		raise ValueError('at least two points are required to fit a line')

	centroid = np.mean(points, axis=0)
	centered_points = points - centroid
	covariance = centered_points.T @ centered_points
	_, eigenvectors = np.linalg.eigh(covariance)
	normal = eigenvectors[:, 0]
	direction = eigenvectors[:, -1]
	signed_residuals = centered_points @ normal
	rms_residual_mm = math.sqrt(float(np.mean(np.square(signed_residuals))))
	max_abs_residual_mm = float(np.max(np.abs(signed_residuals)))
	line_angle_deg = math.degrees(math.atan2(float(direction[1]), float(direction[0])))

	return FittedLine(
		point_x_mm=float(centroid[0]),
		point_z_mm=float(centroid[1]),
		direction_x=float(direction[0]),
		direction_z=float(direction[1]),
		normal_x=float(normal[0]),
		normal_z=float(normal[1]),
		line_angle_deg=line_angle_deg,
		rms_residual_mm=rms_residual_mm,
		max_abs_residual_mm=max_abs_residual_mm,
	)


def fit_alpha_at_value(
	samples: list[LaserAngleCalibrationSample],
	*,
	alpha_deg: float,
	laser_offset_x_mm: float,
	laser_offset_z_mm: float,
	beam_zero_deg: float,
) -> AlphaFitResult:
	points = reconstruct_hit_points(
		samples,
		alpha_deg=alpha_deg,
		laser_offset_x_mm=laser_offset_x_mm,
		laser_offset_z_mm=laser_offset_z_mm,
		beam_zero_deg=beam_zero_deg,
	)
	line = fit_line(points)

	return AlphaFitResult(
		alpha_deg=alpha_deg,
		sample_count=len(samples),
		line=line,
	)


def search_best_alpha(
	samples: list[LaserAngleCalibrationSample],
	*,
	alpha_min_deg: float,
	alpha_max_deg: float,
	coarse_step_deg: float,
	fine_window_deg: float,
	fine_step_deg: float,
	laser_offset_x_mm: float,
	laser_offset_z_mm: float,
	beam_zero_deg: float,
) -> AlphaFitResult:
	if len(samples) < 2:
		raise ValueError('at least two valid samples are required')

	coarse_candidates = build_axis_positions(alpha_min_deg, alpha_max_deg, coarse_step_deg)
	best_result: AlphaFitResult | None = None

	for alpha_deg in coarse_candidates:
		candidate = fit_alpha_at_value(
			samples,
			alpha_deg=alpha_deg,
			laser_offset_x_mm=laser_offset_x_mm,
			laser_offset_z_mm=laser_offset_z_mm,
			beam_zero_deg=beam_zero_deg,
		)

		if best_result is None:
			best_result = candidate
			continue

		if candidate.line.rms_residual_mm < best_result.line.rms_residual_mm:
			best_result = candidate

	if best_result is None:
		raise ValueError('failed to evaluate alpha candidates')

	fine_min_deg = max(alpha_min_deg, best_result.alpha_deg - fine_window_deg)
	fine_max_deg = min(alpha_max_deg, best_result.alpha_deg + fine_window_deg)
	fine_candidates = build_axis_positions(fine_min_deg, fine_max_deg, fine_step_deg)

	for alpha_deg in fine_candidates:
		candidate = fit_alpha_at_value(
			samples,
			alpha_deg=alpha_deg,
			laser_offset_x_mm=laser_offset_x_mm,
			laser_offset_z_mm=laser_offset_z_mm,
			beam_zero_deg=beam_zero_deg,
		)

		if candidate.line.rms_residual_mm < best_result.line.rms_residual_mm:
			best_result = candidate

	return best_result


def intersect_ray_with_line(
	*,
	ray_origin: np.ndarray,
	ray_direction: np.ndarray,
	line_point: np.ndarray,
	line_direction: np.ndarray,
) -> float | None:
	line_normal = build_line_normal(line_direction)
	denominator = float(np.dot(line_normal, ray_direction))

	if abs(denominator) < 1e-9:
		return None

	distance_mm = float(np.dot(line_normal, line_point - ray_origin) / denominator)

	if distance_mm <= 0.0:
		return None

	return distance_mm


def generate_synthetic_samples(
	*,
	axis_x_positions: list[float],
	axis_z_positions: list[float],
	rotation_positions: list[float],
	true_alpha_deg: float,
	laser_offset_x_mm: float,
	laser_offset_z_mm: float,
	beam_zero_deg: float,
	line_point_x_mm: float,
	line_point_z_mm: float,
	line_direction_deg: float,
	noise_std_mm: float,
	seed: int,
) -> list[LaserAngleCalibrationSample]:
	rng = np.random.default_rng(seed)
	line_point = np.array([line_point_x_mm, line_point_z_mm], dtype=np.float64)
	line_direction = build_line_direction(line_direction_deg)
	local_offset = np.array([laser_offset_x_mm, laser_offset_z_mm], dtype=np.float64)
	beam_zero_rad = math.radians(beam_zero_deg)
	samples: list[LaserAngleCalibrationSample] = []

	for axis_x_mm in axis_x_positions:
		for axis_z_mm in axis_z_positions:
			for rotation_deg in rotation_positions:
				effective_angle_rad = math.radians(rotation_deg + true_alpha_deg)
				rotation_matrix = build_rotation_matrix(effective_angle_rad)
				axis_point = np.array([axis_x_mm, axis_z_mm], dtype=np.float64)
				ray_origin = axis_point + (rotation_matrix @ local_offset)
				ray_direction = np.array(
					[
						math.cos(effective_angle_rad + beam_zero_rad),
						math.sin(effective_angle_rad + beam_zero_rad),
					],
					dtype=np.float64,
				)
				distance_mm = intersect_ray_with_line(
					ray_origin=ray_origin,
					ray_direction=ray_direction,
					line_point=line_point,
					line_direction=line_direction,
				)

				if distance_mm is None:
					continue

				if noise_std_mm > 0.0:
					distance_mm += float(rng.normal(0.0, noise_std_mm))

				samples.append(
					LaserAngleCalibrationSample(
						axis_x_mm=float(axis_x_mm),
						axis_z_mm=float(axis_z_mm),
						rotation_deg=float(rotation_deg),
						absolute_rotation_deg=float(rotation_deg),
						stage_extension_mm=0.0,
						z_extension_mm=0.0,
						distance_mm=float(distance_mm),
					)
				)

	return samples


def load_samples(input_path: Path) -> list[LaserAngleCalibrationSample]:
	document = json.loads(input_path.read_text(encoding='utf-8'))
	raw_samples = document
	metadata: dict[str, Any] = {}

	if isinstance(document, dict) is True:
		raw_samples = document.get('samples', [])
		metadata = document.get('metadata', {})

	if isinstance(raw_samples, list) is False:
		raise ValueError('input document must contain a list of samples')

	samples: list[LaserAngleCalibrationSample] = []
	rotation_center_deg = metadata.get('rotation_center_deg')
	default_stage_extension_mm = metadata.get('stage_extension_mm', 0.0)

	for raw_sample in raw_samples:
		rotation_deg = float(raw_sample['rotation_deg'])
		absolute_rotation_deg = raw_sample.get('absolute_rotation_deg')

		if absolute_rotation_deg is None:
			if rotation_center_deg is None:
				absolute_rotation_deg = rotation_deg
			else:
				absolute_rotation_deg = float(rotation_center_deg) + rotation_deg

		samples.append(
			LaserAngleCalibrationSample(
				axis_x_mm=float(raw_sample['axis_x_mm']),
				axis_z_mm=float(raw_sample['axis_z_mm']),
				rotation_deg=rotation_deg,
				absolute_rotation_deg=float(absolute_rotation_deg),
				stage_extension_mm=float(raw_sample.get('stage_extension_mm', default_stage_extension_mm)),
				z_extension_mm=float(raw_sample.get('z_extension_mm', raw_sample['axis_z_mm'])),
				distance_mm=float(raw_sample['distance_mm']),
			)
		)

	return samples


def write_output_document(
	*,
	output_path: Path,
	mode: str,
	samples: list[LaserAngleCalibrationSample],
	fit_result: AlphaFitResult,
	metadata: dict[str, Any],
) -> None:
	output_path.parent.mkdir(parents=True, exist_ok=True)
	output_path.write_text(
		json.dumps(
			{
				'created_at': get_current_timestamp(),
				'mode': mode,
				'metadata': metadata,
				'samples': [asdict(sample) for sample in samples],
				'fit_result': {
					'alpha_deg': fit_result.alpha_deg,
					'sample_count': fit_result.sample_count,
					'line': asdict(fit_result.line),
				},
			},
			indent='\t',
		),
		encoding='utf-8',
	)


def print_fit_result(fit_result: AlphaFitResult) -> None:
	print_status(f'alpha_deg={fit_result.alpha_deg:.4f}')
	print_status(f'sample_count={fit_result.sample_count}')
	print_status(f'rms_residual_mm={fit_result.line.rms_residual_mm:.6f}')
	print_status(f'max_abs_residual_mm={fit_result.line.max_abs_residual_mm:.6f}')
	print_status(f'line_angle_deg={fit_result.line.line_angle_deg:.4f}')


def print_estimated_rotation_from_metadata(
	*,
	fit_result: AlphaFitResult,
	metadata: dict[str, Any],
) -> None:
	estimated_rotation_deg = -float(fit_result.alpha_deg)
	print_status(f'estimated_rotation_deg={estimated_rotation_deg:.4f}')

	rotation_center_deg = metadata.get('rotation_center_deg')

	if rotation_center_deg is None:
		return

	print_status(f'rotation_center_deg={float(rotation_center_deg):.4f}')
	print_status(
		f'rotation_correction_deg={estimated_rotation_deg - float(rotation_center_deg):.4f}'
	)


def build_standalone_config(config: Any) -> dict[str, Any]:
	config_data = config.model_dump(mode='json')
	config_data['robot']['io']['curtain']['enabled'] = False
	tool_config = config_data['robot']['tool']
	tool_config.setdefault('laser', {})
	tool_config['laser']['enabled'] = True
	robot_bus = config_data['robot'].setdefault('bus', {})
	laser_bus = robot_bus.get('laser')
	scale_bus = robot_bus.get('scale')
	scale_enabled = config_data['robot']['io'].get('scale', {}).get('enabled', True)

	if laser_bus is None and scale_enabled is False and scale_bus is not None:
		robot_bus['laser'] = dict(scale_bus)

	return config_data


async def run_with_temp_robot_config(
	*,
	config: Any,
	config_path: str,
	did_run: Any,
	shutdown_action: str,
) -> Any:
	import perpetual_os1.common as common_module
	import perpetual_os1.robot as robot_module
	from perpetual_os1.robot import Robot

	with tempfile.NamedTemporaryFile(
		mode='w',
		suffix='.json',
		prefix='laser-angle-config-',
		dir=str(Path(config_path).resolve().parent),
		delete=False,
	) as temp_config_file:
		temp_config_path = Path(temp_config_file.name)
		json.dump(build_standalone_config(config), temp_config_file, indent='\t')

	original_common_config_path = common_module.CONFIG_PATH
	original_robot_config_path = robot_module.CONFIG_PATH
	common_module.CONFIG_PATH = temp_config_path
	robot_module.CONFIG_PATH = temp_config_path

	try:
		robot = Robot()
		print_status('phase=robot-configure')
		await robot.configure()

		try:
			return await did_run(robot)
		finally:
			if should_skip_motion_cleanup_on_cancel() is False:
				if shutdown_action == 'stop':
					print_status('phase=robot-stop')
					await robot.stop()
				else:
					print_status('phase=robot-shutdown')
					await robot.shutdown()
	finally:
		common_module.CONFIG_PATH = original_common_config_path
		robot_module.CONFIG_PATH = original_robot_config_path
		temp_config_path.unlink(missing_ok=True)


async def sample_laser_depth(
	tool: Any,
	*,
	settle_ms: int,
	samples_per_point: int,
) -> float | None:
	if settle_ms > 0:
		await asyncio.sleep(settle_ms / 1000.0)

	sample_values: list[float] = []

	for _ in range(samples_per_point):
		depth_mm = await tool.get_laser_distance()

		if depth_mm is None:
			continue

		if float(depth_mm) <= GAP_DEPTH_THRESHOLD_MM:
			continue

		sample_values.append(float(depth_mm))

	if len(sample_values) == 0:
		return None

	sample_values.sort()
	return sample_values[len(sample_values) // 2]


async def set_y_brake_for_shelf_scouting(
	tool: Any,
	*,
	is_enabled: bool,
) -> None:
	y_motor = getattr(tool, '_my', None)

	if y_motor is None:
		raise ValueError('y-axis motor is required for shelf scouting')

	if is_enabled is True:
		await y_motor.restore_brake_output()
		print_status('y_brake=enabled')
		return

	await y_motor.disable_brake_output()
	print_status('y_brake=disabled')


def build_shelf_scout_x_positions(
	*,
	scan_config: Any,
	x_min_mm: float,
	x_max_mm: float,
	side_name: str,
	scout_x_mm: float | None,
) -> list[float]:
	if scout_x_mm is not None:
		return [float(scout_x_mm)]

	if side_name == 'right':
		return [RIGHT_MODULE_SHELF_SCOUT_X_MM]

	left_xy_reference = getattr(scan_config, 'left_xy_reference', None)

	if left_xy_reference is not None:
		return [max(float(left_xy_reference.x_mm), x_max_mm)]

	positions: list[float] = []

	for offset_mm in SHELF_SCOUT_X_OFFSETS_MM:
		x_mm = max(x_min_mm, x_max_mm - offset_mm)

		if x_mm in positions:
			continue

		positions.append(x_mm)

	return positions

def get_default_rotation_center_deg(side_name: str) -> float:
	if side_name == 'right':
		return RIGHT_MODULE_ROTATION_CENTER_DEG

	return LEFT_MODULE_ROTATION_CENTER_DEG


async def enter_capture_mode(
	tool: Any,
	*,
	side_name: str,
	side_number: int,
	x_mm: float,
	y_mm: float,
	absolute_rotation_deg: float,
	stage_extension_mm: float,
	z_mm: float,
	z_move_rpm: float,
	z_move_accel: float,
	should_abort: Callable[[], bool] | None = None,
) -> None:
	await tool.retract()

	await prepare_capture_pose(
		tool,
		side_name=side_name,
		side_number=side_number,
		x_mm=x_mm,
		y_mm=y_mm,
		absolute_rotation_deg=absolute_rotation_deg,
		stage_extension_mm=stage_extension_mm,
		z_mm=z_mm,
		z_move_rpm=z_move_rpm,
		z_move_accel=z_move_accel,
		should_rotate=True,
		should_use_safe_right_rotation_path=True,
		should_move_z=True,
		should_abort=should_abort,
	)


async def scan_shelf_scout_line(
	tool: Any,
	*,
	x_mm: float,
	y_positions: list[float],
	settle_ms: int,
	samples_per_point: int,
	progress_label: str,
	stop_after_first_edge: bool = False,
	progress_every: int = 25,
	should_abort: Callable[[], bool] | None = None,
) -> tuple[list[float | None], list[ShelfSurfaceEdge]]:
	if len(y_positions) == 0:
		return [], []

	raise_if_cancelled(should_abort)
	await tool._mx.move_to_position_no_wait(x_mm)
	await tool._my.move_to_position_no_wait(y_positions[0])
	await tool.gantry_wait(should_abort=should_abort)

	depth_values: list[float | None] = [None for _ in y_positions]
	detected_edges: list[ShelfSurfaceEdge] = []
	position_idx = 0
	sampled_count = 0
	run_start_idx: int | None = None
	run_end_idx: int | None = None

	while position_idx < len(y_positions):
		raise_if_cancelled(should_abort)
		y_mm = y_positions[position_idx]

		if position_idx > 0:
			await tool._my.move_to_position_no_wait(y_mm)
			await tool.gantry_wait(should_abort=should_abort)

		raise_if_cancelled(should_abort)
		depth_mm = await sample_laser_depth(
			tool,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
		)
		depth_values[position_idx] = depth_mm
		sampled_count += 1

		if sampled_count % progress_every == 0 or position_idx + 1 == len(y_positions):
			print_status(
				f'{progress_label}={sampled_count}/{len(y_positions)} '
				f'x={x_mm:.2f} '
				f'y={y_mm:.2f} '
				f'depth={depth_to_label(depth_mm)}'
			)

		if is_positive_depth(depth_mm) is True:
			if run_start_idx is None:
				run_start_idx = position_idx

			run_end_idx = position_idx
			position_idx += 1
			continue

		if run_start_idx is None or run_end_idx is None:
			position_idx += 1
			continue

		run_length = run_end_idx - run_start_idx + 1
		band_height_mm = abs(y_positions[run_end_idx] - y_positions[run_start_idx]) + SHELF_SCOUT_STEP_MM
		is_bounded_band = run_start_idx > 0

		if (
			run_length >= SHELF_EDGE_CONFIRMATION_COUNT and
			is_bounded_band is True and
			band_height_mm >= SHELF_BRIM_MIN_HEIGHT_MM and
			band_height_mm <= SHELF_BRIM_MAX_HEIGHT_MM
		):
			center_y_mm = float((y_positions[run_start_idx] + y_positions[run_end_idx]) / 2.0)
			detected_edges.append(
				ShelfSurfaceEdge(
					x_mm=x_mm,
					y_mm=center_y_mm,
				)
			)

			if stop_after_first_edge is True:
				return depth_values, detected_edges

			skip_target_y_mm = y_positions[run_end_idx] - SHELF_SCOUT_POST_EDGE_SKIP_MM

			while position_idx < len(y_positions) and y_positions[position_idx] > skip_target_y_mm:
				position_idx += 1

			run_start_idx = None
			run_end_idx = None
			continue

		run_start_idx = None
		run_end_idx = None
		position_idx += 1

	if run_start_idx is not None and run_end_idx is not None:
		run_length = run_end_idx - run_start_idx + 1
		band_height_mm = abs(y_positions[run_end_idx] - y_positions[run_start_idx]) + SHELF_SCOUT_STEP_MM
		is_bounded_band = run_start_idx > 0 and run_end_idx < len(y_positions) - 1

		if (
			run_length >= SHELF_EDGE_CONFIRMATION_COUNT and
			is_bounded_band is True and
			band_height_mm >= SHELF_BRIM_MIN_HEIGHT_MM and
			band_height_mm <= SHELF_BRIM_MAX_HEIGHT_MM
		):
			center_y_mm = float((y_positions[run_start_idx] + y_positions[run_end_idx]) / 2.0)
			detected_edges.append(
				ShelfSurfaceEdge(
					x_mm=x_mm,
					y_mm=center_y_mm,
				)
			)

			if stop_after_first_edge is True:
				return depth_values, detected_edges

	return depth_values, detected_edges


async def move_gantry_direct(
	tool: Any,
	*,
	x_mm: float,
	y_mm: float,
	should_abort: Callable[[], bool] | None = None,
) -> None:
	await tool._my.move_to_position_no_wait(y_mm)
	await tool._mx.move_to_position_no_wait(x_mm)
	await tool.gantry_wait(should_abort=should_abort)


async def probe_stage_until_stall(
	tool: Any,
	*,
	target_stage_mm: float,
	retract_stage_mm: float,
	settle_ms: int,
	timeout_s: float,
	probe_rpm: int | None,
	no_progress_s: float,
	probe_step_mm: float,
	fast_approach_mm: float,
	fast_approach_rpm: int | None,
	auto_retract: bool = True,
	should_abort: Callable[[], bool] | None = None,
) -> tuple[bool, float, str, int | None]:
	raise_if_cancelled(should_abort)
	await tool._ms.move_to_position(retract_stage_mm)
	await tool._ms.clear_alarm()

	if settle_ms > 0:
		await asyncio.sleep(settle_ms / 1000.0)

	start_time = time.monotonic()
	current_target_mm = retract_stage_mm
	fast_target_mm = min(max(fast_approach_mm, retract_stage_mm), target_stage_mm)

	if fast_target_mm > retract_stage_mm:
		raise_if_cancelled(should_abort)
		await tool._ms.move_to_position(fast_target_mm, rpm=fast_approach_rpm)
		current_target_mm = fast_target_mm

	position_epsilon_mm = 0.02
	target_tolerance_mm = 0.2

	while current_target_mm < target_stage_mm:
		raise_if_cancelled(should_abort)
		next_target_mm = min(current_target_mm + probe_step_mm, target_stage_mm)
		await tool._ms.move_to_position_no_wait(next_target_mm, rpm=probe_rpm)
		last_movement_time = time.monotonic()
		last_position_mm = await tool._ms.get_position()
		has_moved = False

		while True:
			raise_if_cancelled(should_abort)
			await asyncio.sleep(0.03)
			now = time.monotonic()
			stage_position_mm = await tool._ms.get_position()
			alarm_code = await tool._ms.get_param('Current alarm')

			if abs(stage_position_mm - last_position_mm) > position_epsilon_mm:
				last_movement_time = now
				has_moved = True

			last_position_mm = stage_position_mm

			if alarm_code is not None and alarm_code != 0:
				await tool._ms.stop()
				await asyncio.sleep(0.03)
				stage_position_mm = await tool._ms.get_position()
				await tool._ms.clear_alarm()

				if auto_retract is True:
					await tool._ms.move_to_position(retract_stage_mm, rpm=probe_rpm)

				return True, float(stage_position_mm), 'alarm', int(alarm_code)

			if stage_position_mm >= next_target_mm - target_tolerance_mm:
				current_target_mm = next_target_mm
				break

			if has_moved is True and now - last_movement_time > no_progress_s:
				await tool._ms.stop()
				await asyncio.sleep(0.03)
				stage_position_mm = await tool._ms.get_position()
				await tool._ms.clear_alarm()

				if auto_retract is True:
					await tool._ms.move_to_position(retract_stage_mm, rpm=probe_rpm)

				return True, float(stage_position_mm), 'no_progress', alarm_code

			if now - start_time > timeout_s:
				await tool._ms.stop()
				await asyncio.sleep(0.03)
				stage_position_mm = await tool._ms.get_position()
				await tool._ms.clear_alarm()

				if auto_retract is True:
					await tool._ms.move_to_position(retract_stage_mm, rpm=probe_rpm)

				return True, float(stage_position_mm), 'timeout', alarm_code

	stage_position_mm = await tool._ms.get_position()

	if auto_retract is True:
		await tool._ms.move_to_position(retract_stage_mm, rpm=probe_rpm)

	return False, float(stage_position_mm), 'limit', None


def filter_shelf_edges(
	*,
	edges: list[ShelfSurfaceEdge],
	y_min_mm: float,
	y_max_mm: float,
	side_name: str,
	scout_x_positions: list[float],
) -> list[ShelfSurfaceEdge]:
	filtered_edges: list[ShelfSurfaceEdge] = []

	for edge in sorted(edges, key=lambda current: -current.y_mm):
		if edge.y_mm > y_max_mm - SHELF_EDGE_TOP_MARGIN_MM:
			continue

		if len(filtered_edges) == 0:
			filtered_edges.append(edge)
			continue

		if abs(filtered_edges[-1].y_mm - edge.y_mm) < SHELF_MIN_SPACING_MM:
			continue

		filtered_edges.append(edge)

	return filtered_edges


async def prepare_capture_pose(
	tool: Any,
	*,
	side_name: str,
	side_number: int,
	x_mm: float,
	y_mm: float,
	absolute_rotation_deg: float,
	stage_extension_mm: float,
	z_mm: float,
	z_move_rpm: float,
	z_move_accel: float,
	should_rotate: bool,
	should_use_safe_right_rotation_path: bool,
	should_move_z: bool,
	should_abort: Callable[[], bool] | None = None,
) -> None:
	if side_name == 'right' and should_rotate is True and should_use_safe_right_rotation_path is True:
		await move_gantry_direct(
			tool,
			x_mm=RIGHT_MODULE_ROTATION_SAFE_X_MM,
			y_mm=y_mm,
			should_abort=should_abort,
		)
		await tool.ensure_safe_rotation_stage_extension()
		await tool._mr.move_to_position(absolute_rotation_deg)
		await move_gantry_direct(
			tool,
			x_mm=x_mm,
			y_mm=y_mm,
			should_abort=should_abort,
		)
	else:
		await move_gantry_direct(
			tool,
			x_mm=x_mm,
			y_mm=y_mm,
			should_abort=should_abort,
		)

		if should_rotate is True:
			await tool.ensure_safe_rotation_stage_extension()
			await tool._mr.move_to_position(absolute_rotation_deg)

	await tool._ms.ready()
	await tool._ms.move_to_position(stage_extension_mm)

	if should_move_z is True:
		await tool._mg.move_to_position(z_mm, rpm=z_move_rpm, accel=z_move_accel)


async def resolve_capture_y_mm(
	tool: Any,
	*,
	args: argparse.Namespace,
	scan_config: Any,
	side_name: str,
	side_number: int,
	x_positions: list[float],
	rotation_center_deg: float,
	stage_extension_mm: float,
	base_z_mm: float,
	z_move_rpm: float,
	z_move_accel: float,
	settle_ms: int,
	samples_per_point: int,
	should_abort: Callable[[], bool] | None = None,
) -> float:
	raise_if_cancelled(should_abort)
	raw_y_mm = getattr(args, 'y_mm', None)
	raw_y_range = getattr(args, 'y_range', None)
	raw_scout_x_mm = getattr(args, 'scout_x_mm', None)

	if raw_y_mm is not None:
		return float(raw_y_mm)

	if raw_y_range is None:
		raise ValueError('either --y-mm or --y-range is required')

	y_min_mm = float(raw_y_range[0])
	y_max_mm = float(raw_y_range[1])
	scout_x_positions = build_shelf_scout_x_positions(
		scan_config=scan_config,
		x_min_mm=min(x_positions),
		x_max_mm=max(x_positions),
		side_name=side_name,
		scout_x_mm=raw_scout_x_mm,
	)
	scout_y_positions = list(reversed(build_axis_positions(y_min_mm, y_max_mm, SHELF_SCOUT_STEP_MM)))
	edges: list[ShelfSurfaceEdge] = []
	is_y_brake_disabled = False

	if len(scout_y_positions) == 0:
		raise ValueError('scout y range produced no positions')

	try:
		raise_if_cancelled(should_abort)
		await set_y_brake_for_shelf_scouting(tool, is_enabled=False)
		is_y_brake_disabled = True
		await enter_capture_mode(
			tool,
			side_name=side_name,
			side_number=side_number,
			x_mm=float(scout_x_positions[0]),
			y_mm=float(scout_y_positions[0]),
			absolute_rotation_deg=rotation_center_deg,
			stage_extension_mm=stage_extension_mm,
			z_mm=base_z_mm,
			z_move_rpm=z_move_rpm,
			z_move_accel=z_move_accel,
			should_abort=should_abort,
		)
		raise_if_cancelled(should_abort)

		for scout_idx, scout_x_mm in enumerate(scout_x_positions, start=1):
			raise_if_cancelled(should_abort)
			if scout_idx > 1:
				await prepare_capture_pose(
					tool,
					side_name=side_name,
					side_number=side_number,
					x_mm=float(scout_x_mm),
					y_mm=float(scout_y_positions[0]),
					absolute_rotation_deg=rotation_center_deg,
					stage_extension_mm=stage_extension_mm,
					z_mm=base_z_mm,
					z_move_rpm=z_move_rpm,
					z_move_accel=z_move_accel,
					should_rotate=False,
					should_use_safe_right_rotation_path=False,
					should_move_z=False,
					should_abort=should_abort,
				)
				raise_if_cancelled(should_abort)
			print_status(
				f'phase=shelf-scout scout={scout_idx}/{len(scout_x_positions)} x={float(scout_x_mm):.2f}'
			)
			_, scout_edges = await scan_shelf_scout_line(
				tool,
				x_mm=float(scout_x_mm),
				y_positions=scout_y_positions,
				settle_ms=settle_ms,
				samples_per_point=samples_per_point,
				progress_label=f'shelf_scout_{scout_idx}',
				stop_after_first_edge=True,
				should_abort=should_abort,
			)
			edges.extend(scout_edges)
			raise_if_cancelled(should_abort)

			if len(scout_edges) > 0:
				break
	finally:
		if is_y_brake_disabled is True:
			await set_y_brake_for_shelf_scouting(tool, is_enabled=True)

	filtered_edges = filter_shelf_edges(
		edges=edges,
		y_min_mm=y_min_mm,
		y_max_mm=y_max_mm,
		side_name=side_name,
		scout_x_positions=scout_x_positions,
	)
	print_status(
		'shelf_edges='
		f'{len(filtered_edges)} '
		f'y={[round(edge.y_mm, 2) for edge in filtered_edges]}'
	)

	if len(filtered_edges) == 0:
		raise ValueError('no shelf surface detected during scout')

	target_y_mm = float(filtered_edges[0].y_mm)
	print_status(f'angle_capture_y_mm={target_y_mm:.2f}')
	return target_y_mm


async def capture_samples_with_robot(args: argparse.Namespace) -> tuple[list[LaserAngleCalibrationSample], dict[str, Any]]:
	from perpetual_os1.common import load_config_file

	config = load_config_file(args.config)
	scan_config = config.robot.grid.scan
	settle_ms = int(args.settle_ms if args.settle_ms is not None else scan_config.settle_ms)
	samples_per_point = int(
		args.samples_per_point if args.samples_per_point is not None else scan_config.samples_per_point
	)
	x_positions = build_axis_positions(args.x_range[0], args.x_range[1], args.x_range[2])
	z_offset_positions = build_axis_positions(
		args.z_offset_range[0],
		args.z_offset_range[1],
		args.z_offset_range[2],
	)
	rotation_positions = build_axis_positions(
		args.rotation_range[0],
		args.rotation_range[1],
		args.rotation_range[2],
	)
	stage_extension_mm = float(
		args.stage_extension_mm
		if args.stage_extension_mm is not None
		else (
			scan_config.scan_stage_extension_mm
			if scan_config.scan_stage_extension_mm is not None
			else LASER_SCAN_STAGE_EXTENSION_MM
		)
	)
	scan_z_offset_mm = float(
		scan_config.scan_z_offset_mm
		if scan_config.scan_z_offset_mm is not None
		else LASER_SCAN_Z_OFFSET_MM
	)
	samples: list[LaserAngleCalibrationSample] = []

	async def did_run(robot: Any) -> tuple[list[LaserAngleCalibrationSample], dict[str, Any]]:
		tool = robot._tool

		if tool._laser_enabled is False or tool._laser is None:
			raise ValueError('laser is not enabled')

		if tool._mx is None or tool._my is None or tool._mg is None or tool._ms is None or tool._mr is None:
			raise ValueError('x, y, z, stage, and rotation motors are required')

		side_name = args.module
		side_number = 0 if side_name == 'left' else 1
		rotation_center_deg = float(
			args.rotation_center_deg
			if args.rotation_center_deg is not None
			else get_default_rotation_center_deg(side_name)
		)

		for rotation_deg in rotation_positions:
			absolute_rotation_deg = rotation_center_deg + float(rotation_deg)

			if absolute_rotation_deg < 0.0 or absolute_rotation_deg > 180.0:
				raise ValueError(
					'rotation sweep exceeds motor range '
					f'(absolute target {absolute_rotation_deg:.3f} deg is outside 0..180)'
				)

		z_extension_place = await tool.get_param('Z_EXTENSION_PLACE')
		z_move_rpm = await tool.get_param('Z_MOVE_RPM')
		z_move_accel = await tool.get_param('Z_MOVE_ACCEL')

		if z_extension_place is None or z_move_rpm is None or z_move_accel is None:
			raise ValueError('Z_EXTENSION_PLACE, Z_MOVE_RPM, and Z_MOVE_ACCEL are required')

		base_z_mm = float(
			args.base_z_mm
			if args.base_z_mm is not None
			else float(z_extension_place) + scan_z_offset_mm
		)
		current_z_mm: float | None = None
		await tool.retract()
		capture_y_mm = await resolve_capture_y_mm(
			tool,
			args=args,
			scan_config=scan_config,
			side_name=side_name,
			side_number=side_number,
			x_positions=x_positions,
			rotation_center_deg=rotation_center_deg,
			stage_extension_mm=stage_extension_mm,
			base_z_mm=base_z_mm,
			z_move_rpm=float(z_move_rpm),
			z_move_accel=float(z_move_accel),
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
		)
		current_z_mm = base_z_mm
		current_absolute_rotation_deg: float | None = rotation_center_deg

		for z_offset_mm in z_offset_positions:
			target_z_mm = base_z_mm + float(z_offset_mm)

			for x_idx, x_mm in enumerate(x_positions, start=1):
				for rotation_idx, rotation_deg in enumerate(rotation_positions, start=1):
					absolute_rotation_deg = rotation_center_deg + float(rotation_deg)
					print_status(
						f'phase=capture x={x_idx}/{len(x_positions)} '
						f'z={z_offset_mm:.2f} '
						f'rotation={rotation_idx}/{len(rotation_positions)} '
						f'absolute_rotation_deg={absolute_rotation_deg:.3f}'
					)
					await prepare_capture_pose(
						tool,
						side_name=side_name,
						side_number=side_number,
						x_mm=float(x_mm),
						y_mm=capture_y_mm,
						absolute_rotation_deg=absolute_rotation_deg,
						stage_extension_mm=stage_extension_mm,
						z_mm=target_z_mm,
						z_move_rpm=float(z_move_rpm),
						z_move_accel=float(z_move_accel),
						should_rotate=current_absolute_rotation_deg != absolute_rotation_deg,
						should_use_safe_right_rotation_path=False,
						should_move_z=current_z_mm != target_z_mm,
					)
					current_z_mm = target_z_mm
					current_absolute_rotation_deg = absolute_rotation_deg
					depth_mm = await sample_laser_depth(
						tool,
						settle_ms=settle_ms,
						samples_per_point=samples_per_point,
					)

					if depth_mm is None:
						print_status(
							f'sample=invalid x={x_mm:.3f} '
							f'z={target_z_mm:.3f} '
							f'rotation_deg={rotation_deg:.3f}'
						)
						continue

					samples.append(
						LaserAngleCalibrationSample(
							axis_x_mm=float(x_mm),
							axis_z_mm=target_z_mm,
							rotation_deg=float(rotation_deg),
							absolute_rotation_deg=float(absolute_rotation_deg),
							stage_extension_mm=stage_extension_mm,
							z_extension_mm=target_z_mm,
							distance_mm=float(depth_mm),
						)
					)
					print_status(
						f'sample=ok x={x_mm:.3f} '
						f'z={target_z_mm:.3f} '
						f'rotation_deg={rotation_deg:.3f} '
						f'distance_mm={depth_mm:.3f}'
					)

		await tool.retract()

		return samples, {
			'config_path': str(Path(args.config).resolve()),
			'module': side_name,
			'y_mm': float(capture_y_mm),
			'y_range': None if args.y_range is None else [float(args.y_range[0]), float(args.y_range[1])],
			'x_positions': x_positions,
			'z_offset_positions': z_offset_positions,
			'rotation_positions': rotation_positions,
			'stage_extension_mm': stage_extension_mm,
			'base_z_mm': base_z_mm,
			'rotation_center_deg': rotation_center_deg,
			'settle_ms': settle_ms,
			'samples_per_point': samples_per_point,
		}

	return await run_with_temp_robot_config(
		config=config,
		config_path=args.config,
		did_run=did_run,
		shutdown_action=args.shutdown_action,
	)


def resolve_probe_x_mm(
	*,
	args: argparse.Namespace,
	scan_config: Any,
	side_name: str,
) -> float:
	if args.probe_x_mm is not None:
		return float(args.probe_x_mm)

	if args.scout_x_mm is not None:
		return float(args.scout_x_mm)

	scout_x_positions = build_shelf_scout_x_positions(
		scan_config=scan_config,
		x_min_mm=0.0,
		x_max_mm=0.0,
		side_name=side_name,
		scout_x_mm=None,
	)
	return float(scout_x_positions[0])


def summarize_stage_stall_samples(
	samples: list[StageStallProbeSample],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
	rotation_buckets: dict[float, list[float]] = {}
	rotation_metadata: dict[float, dict[str, Any]] = {}

	for sample in samples:
		bucket = rotation_buckets.setdefault(sample.rotation_deg, [])

		if sample.stalled is True:
			bucket.append(sample.stage_position_mm)

		rotation_metadata[sample.rotation_deg] = {
			'rotation_deg': float(sample.rotation_deg),
			'absolute_rotation_deg': float(sample.absolute_rotation_deg),
		}

	angle_summaries: list[dict[str, Any]] = []
	best_summary: dict[str, Any] | None = None

	for rotation_deg in sorted(rotation_metadata):
		stage_positions = rotation_buckets.get(rotation_deg, [])
		metadata = rotation_metadata[rotation_deg]
		summary = {
			'rotation_deg': metadata['rotation_deg'],
			'absolute_rotation_deg': metadata['absolute_rotation_deg'],
			'stall_count': len(stage_positions),
			'median_stage_position_mm': (
				None if len(stage_positions) == 0 else float(np.median(np.array(stage_positions, dtype=np.float64)))
			),
			'min_stage_position_mm': None if len(stage_positions) == 0 else float(min(stage_positions)),
			'max_stage_position_mm': None if len(stage_positions) == 0 else float(max(stage_positions)),
		}
		angle_summaries.append(summary)

		if summary['median_stage_position_mm'] is None:
			continue

		if best_summary is None:
			best_summary = summary
			continue

		if float(summary['median_stage_position_mm']) > float(best_summary['median_stage_position_mm']):
			best_summary = summary

	return angle_summaries, best_summary


def should_stop_after_stage_drop(
	*,
	best_stage_position_mm: float | None,
	current_stage_position_mm: float | None,
	previous_stage_position_mm: float | None,
	fall_threshold_mm: float,
	fall_run_count: int,
	stop_after_fall_count: int,
) -> tuple[bool, int]:
	if best_stage_position_mm is None:
		return False, 0

	if current_stage_position_mm is None or previous_stage_position_mm is None:
		return False, 0

	if current_stage_position_mm >= best_stage_position_mm - fall_threshold_mm:
		return False, 0

	if current_stage_position_mm >= previous_stage_position_mm:
		return False, 0

	updated_run_count = fall_run_count + 1
	return updated_run_count >= stop_after_fall_count, updated_run_count


def write_stage_stall_output_document(
	*,
	output_path: Path,
	metadata: dict[str, Any],
	samples: list[StageStallProbeSample],
	angle_summaries: list[dict[str, Any]],
	best_summary: dict[str, Any] | None,
) -> None:
	output_path.parent.mkdir(parents=True, exist_ok=True)
	output_path.write_text(
		json.dumps(
			{
				'created_at': get_current_timestamp(),
				'mode': 'stage-stall',
				'metadata': metadata,
				'samples': [asdict(sample) for sample in samples],
				'angle_summaries': angle_summaries,
				'best_summary': best_summary,
			},
			indent='\t',
		),
		encoding='utf-8',
	)


async def run_stage_stall_capture(args: argparse.Namespace) -> tuple[list[StageStallProbeSample], dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
	from perpetual_os1.common import load_config_file

	config = load_config_file(args.config)
	scan_config = config.robot.grid.scan
	settle_ms = int(args.settle_ms if args.settle_ms is not None else scan_config.settle_ms)
	samples_per_point = int(
		args.samples_per_point if args.samples_per_point is not None else scan_config.samples_per_point
	)
	rotation_positions = build_axis_positions(
		args.rotation_range[0],
		args.rotation_range[1],
		args.rotation_range[2],
	)
	stage_extension_mm = float(
		args.stage_extension_mm
		if args.stage_extension_mm is not None
		else (
			scan_config.scan_stage_extension_mm
			if scan_config.scan_stage_extension_mm is not None
			else LASER_SCAN_STAGE_EXTENSION_MM
		)
	)
	scan_z_offset_mm = float(
		scan_config.scan_z_offset_mm
		if scan_config.scan_z_offset_mm is not None
		else LASER_SCAN_Z_OFFSET_MM
	)
	samples: list[StageStallProbeSample] = []

	async def did_run(robot: Any) -> tuple[list[StageStallProbeSample], dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
		tool = robot._tool

		if tool._laser_enabled is False or tool._laser is None:
			raise ValueError('laser is not enabled')

		if tool._mx is None or tool._my is None or tool._mg is None or tool._ms is None or tool._mr is None:
			raise ValueError('x, y, z, stage, and rotation motors are required')

		side_name = args.module
		side_number = 0 if side_name == 'left' else 1
		rotation_center_deg = float(
			args.rotation_center_deg
			if args.rotation_center_deg is not None
			else get_default_rotation_center_deg(side_name)
		)

		for rotation_deg in rotation_positions:
			absolute_rotation_deg = rotation_center_deg + float(rotation_deg)

			if absolute_rotation_deg < 0.0 or absolute_rotation_deg > 180.0:
				raise ValueError(
					'rotation sweep exceeds motor range '
					f'(absolute target {absolute_rotation_deg:.3f} deg is outside 0..180)'
				)

		z_extension_place = await tool.get_param('Z_EXTENSION_PLACE')
		z_move_rpm = await tool.get_param('Z_MOVE_RPM')
		z_move_accel = await tool.get_param('Z_MOVE_ACCEL')

		if z_extension_place is None or z_move_rpm is None or z_move_accel is None:
			raise ValueError('Z_EXTENSION_PLACE, Z_MOVE_RPM, and Z_MOVE_ACCEL are required')

		base_z_mm = float(
			args.base_z_mm
			if args.base_z_mm is not None
			else float(z_extension_place) + scan_z_offset_mm
		)
		probe_x_mm = resolve_probe_x_mm(args=args, scan_config=scan_config, side_name=side_name)
		capture_y_mm = await resolve_capture_y_mm(
			tool,
			args=args,
			scan_config=scan_config,
			side_name=side_name,
			side_number=side_number,
			x_positions=[probe_x_mm],
			rotation_center_deg=rotation_center_deg,
			stage_extension_mm=stage_extension_mm,
			base_z_mm=base_z_mm,
			z_move_rpm=float(z_move_rpm),
			z_move_accel=float(z_move_accel),
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
		)
		stage_limit_mm = float(tool._ms._params.lim)
		probe_target_mm = float(args.stage_target_mm if args.stage_target_mm is not None else stage_limit_mm)
		probe_start_mm = float(args.stage_start_mm)
		default_probe_rpm = min(int(tool._ms._params.rpm), 100)
		probe_rpm = int(args.stage_probe_rpm if args.stage_probe_rpm is not None else default_probe_rpm)
		fast_approach_rpm = int(args.fast_approach_rpm if args.fast_approach_rpm is not None else int(tool._ms._params.rpm))
		probe_step_mm = float(args.probe_step_mm)
		fall_run_count = 0
		best_stage_position_mm: float | None = None
		previous_stage_position_mm: float | None = None

		await enter_capture_mode(
			tool,
			side_name=side_name,
			side_number=side_number,
			x_mm=probe_x_mm,
			y_mm=capture_y_mm,
			absolute_rotation_deg=rotation_center_deg + float(rotation_positions[0]),
			stage_extension_mm=probe_start_mm,
			z_mm=base_z_mm,
			z_move_rpm=float(z_move_rpm),
			z_move_accel=float(z_move_accel),
		)

		for rotation_idx, rotation_deg in enumerate(rotation_positions, start=1):
			absolute_rotation_deg = rotation_center_deg + float(rotation_deg)
			print_status(
				f'phase=stage-stall rotation={rotation_idx}/{len(rotation_positions)} '
				f'absolute_rotation_deg={absolute_rotation_deg:.3f}'
			)
			await prepare_capture_pose(
				tool,
				side_name=side_name,
				side_number=side_number,
				x_mm=probe_x_mm,
				y_mm=capture_y_mm,
				absolute_rotation_deg=absolute_rotation_deg,
				stage_extension_mm=probe_start_mm,
				z_mm=base_z_mm,
				z_move_rpm=float(z_move_rpm),
				z_move_accel=float(z_move_accel),
				should_rotate=True,
				should_use_safe_right_rotation_path=False,
				should_move_z=False,
			)
			did_stall, stage_position_mm, status, alarm_code = await probe_stage_until_stall(
				tool,
				target_stage_mm=probe_target_mm,
				retract_stage_mm=probe_start_mm,
				settle_ms=settle_ms,
				timeout_s=float(args.stage_timeout_s),
				probe_rpm=probe_rpm,
				no_progress_s=float(args.no_progress_timeout_s),
				probe_step_mm=probe_step_mm,
				fast_approach_mm=float(args.fast_approach_mm),
				fast_approach_rpm=fast_approach_rpm,
			)
			samples.append(
				StageStallProbeSample(
					rotation_deg=float(rotation_deg),
					absolute_rotation_deg=absolute_rotation_deg,
					probe_x_mm=probe_x_mm,
					probe_y_mm=capture_y_mm,
					stage_position_mm=stage_position_mm,
					stalled=did_stall,
					status=status,
					alarm_code=None if alarm_code is None else int(alarm_code),
				)
			)

			if did_stall is True:
				print_status(
					f'stage_probe=stall rotation_deg={float(rotation_deg):.3f} '
					f'absolute_rotation_deg={absolute_rotation_deg:.3f} '
					f'stage_position_mm={stage_position_mm:.3f}'
				)

				if best_stage_position_mm is None or stage_position_mm > best_stage_position_mm:
					best_stage_position_mm = stage_position_mm

				should_stop, fall_run_count = should_stop_after_stage_drop(
					best_stage_position_mm=best_stage_position_mm,
					current_stage_position_mm=stage_position_mm,
					previous_stage_position_mm=previous_stage_position_mm,
					fall_threshold_mm=float(args.fall_threshold_mm),
					fall_run_count=fall_run_count,
					stop_after_fall_count=int(args.stop_after_fall_count),
				)
				previous_stage_position_mm = stage_position_mm

				if should_stop is True:
					print_status('stage_probe=stop reason=stage_position_falling')
					break
				continue

			print_status(
				f'stage_probe=skip rotation_deg={float(rotation_deg):.3f} '
				f'absolute_rotation_deg={absolute_rotation_deg:.3f} '
				f'status={status} stage_position_mm={stage_position_mm:.3f}'
			)
			fall_run_count = 0

		await tool.retract()
		angle_summaries, best_summary = summarize_stage_stall_samples(samples)

		return samples, {
			'config_path': str(Path(args.config).resolve()),
			'module': side_name,
			'probe_x_mm': probe_x_mm,
			'y_mm': capture_y_mm,
			'y_range': None if args.y_range is None else [float(args.y_range[0]), float(args.y_range[1])],
			'rotation_positions': rotation_positions,
			'rotation_center_deg': rotation_center_deg,
			'stage_limit_mm': stage_limit_mm,
			'stage_start_mm': probe_start_mm,
			'stage_target_mm': probe_target_mm,
			'stage_probe_rpm': probe_rpm,
			'fast_approach_mm': float(args.fast_approach_mm),
			'fast_approach_rpm': fast_approach_rpm,
			'probe_step_mm': probe_step_mm,
			'no_progress_timeout_s': float(args.no_progress_timeout_s),
			'base_z_mm': base_z_mm,
			'settle_ms': settle_ms,
			'samples_per_point': samples_per_point,
		}, angle_summaries, best_summary

	return await run_with_temp_robot_config(
		config=config,
		config_path=args.config,
		did_run=did_run,
		shutdown_action=args.shutdown_action,
	)


def run_fit(args: argparse.Namespace) -> int:
	input_path = Path(args.input)
	input_document = json.loads(input_path.read_text(encoding='utf-8'))
	input_metadata = input_document.get('metadata', {}) if isinstance(input_document, dict) is True else {}
	samples = load_samples(input_path)
	fit_result = search_best_alpha(
		samples,
		alpha_min_deg=float(args.alpha_min_deg),
		alpha_max_deg=float(args.alpha_max_deg),
		coarse_step_deg=float(args.coarse_step_deg),
		fine_window_deg=float(args.fine_window_deg),
		fine_step_deg=float(args.fine_step_deg),
		laser_offset_x_mm=float(args.laser_offset_x_mm),
		laser_offset_z_mm=float(args.laser_offset_z_mm),
		beam_zero_deg=float(args.beam_zero_deg),
	)
	print_fit_result(fit_result)
	print_estimated_rotation_from_metadata(fit_result=fit_result, metadata=input_metadata)

	if args.output is not None:
		write_output_document(
			output_path=Path(args.output),
			mode='fit',
			samples=samples,
			fit_result=fit_result,
			metadata={
				**input_metadata,
				'input': str(input_path.resolve()),
				'laser_offset_x_mm': float(args.laser_offset_x_mm),
				'laser_offset_z_mm': float(args.laser_offset_z_mm),
				'beam_zero_deg': float(args.beam_zero_deg),
			},
		)

	return 0


def run_simulate(args: argparse.Namespace) -> int:
	axis_x_positions = build_axis_positions(
		args.axis_x_range[0],
		args.axis_x_range[1],
		args.axis_x_range[2],
	)
	axis_z_positions = build_axis_positions(
		args.axis_z_range[0],
		args.axis_z_range[1],
		args.axis_z_range[2],
	)
	rotation_positions = build_axis_positions(
		args.rotation_range[0],
		args.rotation_range[1],
		args.rotation_range[2],
	)
	samples = generate_synthetic_samples(
		axis_x_positions=axis_x_positions,
		axis_z_positions=axis_z_positions,
		rotation_positions=rotation_positions,
		true_alpha_deg=float(args.true_alpha_deg),
		laser_offset_x_mm=float(args.laser_offset_x_mm),
		laser_offset_z_mm=float(args.laser_offset_z_mm),
		beam_zero_deg=float(args.beam_zero_deg),
		line_point_x_mm=float(args.line_point_x_mm),
		line_point_z_mm=float(args.line_point_z_mm),
		line_direction_deg=float(args.line_direction_deg),
		noise_std_mm=float(args.noise_std_mm),
		seed=int(args.seed),
	)
	fit_result = search_best_alpha(
		samples,
		alpha_min_deg=float(args.alpha_min_deg),
		alpha_max_deg=float(args.alpha_max_deg),
		coarse_step_deg=float(args.coarse_step_deg),
		fine_window_deg=float(args.fine_window_deg),
		fine_step_deg=float(args.fine_step_deg),
		laser_offset_x_mm=float(args.laser_offset_x_mm),
		laser_offset_z_mm=float(args.laser_offset_z_mm),
		beam_zero_deg=float(args.beam_zero_deg),
	)
	alpha_error_deg = abs(float(args.true_alpha_deg) - fit_result.alpha_deg)
	print_status(f'true_alpha_deg={float(args.true_alpha_deg):.4f}')
	print_fit_result(fit_result)
	print_status(f'alpha_error_deg={alpha_error_deg:.6f}')

	if args.output is not None:
		write_output_document(
			output_path=Path(args.output),
			mode='simulate',
			samples=samples,
			fit_result=fit_result,
			metadata={
				'true_alpha_deg': float(args.true_alpha_deg),
				'noise_std_mm': float(args.noise_std_mm),
				'seed': int(args.seed),
				'line_point_x_mm': float(args.line_point_x_mm),
				'line_point_z_mm': float(args.line_point_z_mm),
				'line_direction_deg': float(args.line_direction_deg),
			},
		)

	if args.assert_max_alpha_error_deg is not None:
		if alpha_error_deg > float(args.assert_max_alpha_error_deg):
			print_status(
				f'alpha_error_check=failed max={float(args.assert_max_alpha_error_deg):.6f}'
			)
			return 1

	return 0


async def run_capture_async(args: argparse.Namespace) -> int:
	samples, capture_metadata = await capture_samples_with_robot(args)
	fit_result = search_best_alpha(
		samples,
		alpha_min_deg=float(args.alpha_min_deg),
		alpha_max_deg=float(args.alpha_max_deg),
		coarse_step_deg=float(args.coarse_step_deg),
		fine_window_deg=float(args.fine_window_deg),
		fine_step_deg=float(args.fine_step_deg),
		laser_offset_x_mm=float(args.laser_offset_x_mm),
		laser_offset_z_mm=float(args.laser_offset_z_mm),
		beam_zero_deg=float(args.beam_zero_deg),
	)
	print_fit_result(fit_result)
	print_estimated_rotation_from_metadata(fit_result=fit_result, metadata=capture_metadata)

	if args.output is not None:
		write_output_document(
			output_path=Path(args.output),
			mode='capture',
			samples=samples,
			fit_result=fit_result,
			metadata={
				**capture_metadata,
				'laser_offset_x_mm': float(args.laser_offset_x_mm),
				'laser_offset_z_mm': float(args.laser_offset_z_mm),
				'beam_zero_deg': float(args.beam_zero_deg),
			},
		)

	return 0


async def run_stage_stall_async(args: argparse.Namespace) -> int:
	samples, metadata, angle_summaries, best_summary = await run_stage_stall_capture(args)

	if best_summary is None:
		print_status('best_rotation_deg=none')
	else:
		print_status(f"best_rotation_deg={float(best_summary['rotation_deg']):.4f}")
		print_status(f"best_absolute_rotation_deg={float(best_summary['absolute_rotation_deg']):.4f}")
		print_status(f"best_stage_position_mm={float(best_summary['median_stage_position_mm']):.4f}")

	print_status(f'sample_count={len(samples)}')

	if args.output is not None:
		write_stage_stall_output_document(
			output_path=Path(args.output),
			metadata=metadata,
			samples=samples,
			angle_summaries=angle_summaries,
			best_summary=best_summary,
		)

	return 0


def parse_range_argument(value: list[float]) -> tuple[float, float, float]:
	if len(value) != 3:
		raise ValueError('range arguments require MIN MAX STEP')

	return float(value[0]), float(value[1]), float(value[2])


def parse_pair_argument(value: list[float]) -> tuple[float, float]:
	if len(value) != 2:
		raise ValueError('pair arguments require MIN MAX')

	return float(value[0]), float(value[1])


def add_fit_arguments(parser: argparse.ArgumentParser) -> None:
	parser.add_argument('--laser-offset-x-mm', type=float, default=0.0)
	parser.add_argument('--laser-offset-z-mm', type=float, default=0.0)
	parser.add_argument('--beam-zero-deg', type=float, default=0.0)
	parser.add_argument('--alpha-min-deg', type=float, default=-10.0)
	parser.add_argument('--alpha-max-deg', type=float, default=10.0)
	parser.add_argument('--coarse-step-deg', type=float, default=0.1)
	parser.add_argument('--fine-window-deg', type=float, default=0.3)
	parser.add_argument('--fine-step-deg', type=float, default=0.005)
	parser.add_argument('--output', type=str)


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description='Capture or fit laser rotation alpha using flat-surface line residuals.'
	)
	subparsers = parser.add_subparsers(dest='command', required=True)

	fit_parser = subparsers.add_parser('fit')
	fit_parser.add_argument('--input', required=True, type=str)
	add_fit_arguments(fit_parser)

	simulate_parser = subparsers.add_parser('simulate')
	simulate_parser.add_argument('--true-alpha-deg', type=float, default=4.0)
	simulate_parser.add_argument('--noise-std-mm', type=float, default=0.05)
	simulate_parser.add_argument('--seed', type=int, default=0)
	simulate_parser.add_argument('--line-point-x-mm', type=float, default=160.0)
	simulate_parser.add_argument('--line-point-z-mm', type=float, default=0.0)
	simulate_parser.add_argument('--line-direction-deg', type=float, default=90.0)
	simulate_parser.add_argument('--axis-x-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, default=[0.0, 40.0, 10.0])
	simulate_parser.add_argument('--axis-z-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, default=[-8.0, 8.0, 4.0])
	simulate_parser.add_argument('--rotation-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, default=[-8.0, 8.0, 1.0])
	simulate_parser.add_argument('--assert-max-alpha-error-deg', type=float)
	add_fit_arguments(simulate_parser)

	capture_parser = subparsers.add_parser('capture')
	capture_parser.add_argument('--module', choices=['left', 'right'], required=True)
	capture_parser.add_argument('--y-mm', type=float)
	capture_parser.add_argument('--y-range', nargs=2, metavar=('MIN', 'MAX'), type=float)
	capture_parser.add_argument('--scout-x-mm', type=float)
	capture_parser.add_argument('--x-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, required=True)
	capture_parser.add_argument('--z-offset-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, required=True)
	capture_parser.add_argument('--rotation-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, required=True)
	capture_parser.add_argument('--config', type=str, default='data/config.json')
	capture_parser.add_argument('--settle-ms', type=int)
	capture_parser.add_argument('--samples-per-point', type=int)
	capture_parser.add_argument('--stage-extension-mm', type=float)
	capture_parser.add_argument('--base-z-mm', type=float)
	capture_parser.add_argument('--rotation-center-deg', type=float)
	capture_parser.add_argument('--shutdown-action', choices=['stop', 'shutdown'], default='shutdown')
	add_fit_arguments(capture_parser)

	stage_stall_parser = subparsers.add_parser('stage-stall')
	stage_stall_parser.add_argument('--module', choices=['left', 'right'], required=True)
	stage_stall_parser.add_argument('--y-range', nargs=2, metavar=('MIN', 'MAX'), type=float, required=True)
	stage_stall_parser.add_argument('--scout-x-mm', type=float)
	stage_stall_parser.add_argument('--probe-x-mm', type=float)
	stage_stall_parser.add_argument('--rotation-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, required=True)
	stage_stall_parser.add_argument('--config', type=str, default='data/config.json')
	stage_stall_parser.add_argument('--settle-ms', type=int)
	stage_stall_parser.add_argument('--samples-per-point', type=int)
	stage_stall_parser.add_argument('--stage-extension-mm', type=float)
	stage_stall_parser.add_argument('--base-z-mm', type=float)
	stage_stall_parser.add_argument('--rotation-center-deg', type=float)
	stage_stall_parser.add_argument('--shutdown-action', choices=['stop', 'shutdown'], default='shutdown')
	stage_stall_parser.add_argument('--stage-start-mm', type=float, default=3.0)
	stage_stall_parser.add_argument('--stage-target-mm', type=float)
	stage_stall_parser.add_argument('--stage-probe-rpm', type=int)
	stage_stall_parser.add_argument('--fast-approach-mm', type=float, default=40.0)
	stage_stall_parser.add_argument('--fast-approach-rpm', type=int)
	stage_stall_parser.add_argument('--probe-step-mm', type=float, default=0.5)
	stage_stall_parser.add_argument('--stage-timeout-s', type=float, default=15.0)
	stage_stall_parser.add_argument('--no-progress-timeout-s', type=float, default=0.2)
	stage_stall_parser.add_argument('--fall-threshold-mm', type=float, default=0.15)
	stage_stall_parser.add_argument('--stop-after-fall-count', type=int, default=1)
	stage_stall_parser.add_argument('--output', type=str)

	return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
	if args.command == 'simulate':
		args.axis_x_range = parse_range_argument(args.axis_x_range)
		args.axis_z_range = parse_range_argument(args.axis_z_range)
		args.rotation_range = parse_range_argument(args.rotation_range)

	if args.command == 'capture':
		if args.y_range is not None:
			args.y_range = parse_pair_argument(args.y_range)

		args.x_range = parse_range_argument(args.x_range)
		args.z_offset_range = parse_range_argument(args.z_offset_range)
		args.rotation_range = parse_range_argument(args.rotation_range)

		if args.y_mm is None and args.y_range is None:
			raise ValueError('either --y-mm or --y-range is required for capture')

	if args.command == 'stage-stall':
		args.y_range = parse_pair_argument(args.y_range)
		args.rotation_range = parse_range_argument(args.rotation_range)

		if args.stage_start_mm < 0.0:
			raise ValueError('--stage-start-mm must be non-negative')

		if args.stage_target_mm is not None and args.stage_target_mm <= 0.0:
			raise ValueError('--stage-target-mm must be positive')

		if args.stage_timeout_s <= 0.0:
			raise ValueError('--stage-timeout-s must be positive')

		if args.no_progress_timeout_s <= 0.0:
			raise ValueError('--no-progress-timeout-s must be positive')

		if args.stage_probe_rpm is not None and args.stage_probe_rpm <= 0:
			raise ValueError('--stage-probe-rpm must be positive')

		if args.fast_approach_mm < 0.0:
			raise ValueError('--fast-approach-mm must be non-negative')

		if args.fast_approach_rpm is not None and args.fast_approach_rpm <= 0:
			raise ValueError('--fast-approach-rpm must be positive')

		if args.probe_step_mm <= 0.0:
			raise ValueError('--probe-step-mm must be positive')

		if args.fall_threshold_mm < 0.0:
			raise ValueError('--fall-threshold-mm must be non-negative')

		if args.stop_after_fall_count <= 0:
			raise ValueError('--stop-after-fall-count must be greater than zero')

	if args.command in {'fit', 'simulate', 'capture'}:
		if args.alpha_max_deg < args.alpha_min_deg:
			raise ValueError('--alpha-min-deg must be <= --alpha-max-deg')

		if args.coarse_step_deg <= 0.0:
			raise ValueError('--coarse-step-deg must be positive')

		if args.fine_step_deg <= 0.0:
			raise ValueError('--fine-step-deg must be positive')

		if args.fine_window_deg <= 0.0:
			raise ValueError('--fine-window-deg must be positive')


def main() -> int:
	args = parse_args()
	validate_args(args)

	if args.command == 'fit':
		return run_fit(args)

	if args.command == 'simulate':
		return run_simulate(args)

	if args.command == 'capture':
		return asyncio.run(run_capture_async(args))

	if args.command == 'stage-stall':
		return asyncio.run(run_stage_stall_async(args))

	raise ValueError(f'unsupported command: {args.command}')


if __name__ == '__main__':
	raise SystemExit(main())
