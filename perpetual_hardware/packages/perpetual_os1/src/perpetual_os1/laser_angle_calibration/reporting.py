from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from .models import AlphaFitResult, LaserAngleCalibrationSample, StageStallProbeSample
from .status import get_current_timestamp, print_status


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
