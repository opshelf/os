from __future__ import annotations

import math

import numpy as np

from .models import AlphaFitResult, FittedLine, LaserAngleCalibrationSample


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
