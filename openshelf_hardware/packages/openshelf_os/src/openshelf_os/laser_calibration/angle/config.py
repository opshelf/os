from __future__ import annotations

from typing import Any

from .constants import LASER_SCAN_STAGE_EXTENSION_MM, LASER_SCAN_Z_OFFSET_MM


def get_laser_sample_settle_ms(scan_config: Any) -> int:
	return int(scan_config.laser_sample_settle_ms)


def get_laser_samples_per_point(scan_config: Any) -> int:
	return int(scan_config.laser_samples_per_point)


def get_laser_scan_pose_stage_mm(scan_config: Any) -> float:
	configured_value = getattr(scan_config, 'laser_scan_pose_stage_mm', None)

	if configured_value is None:
		return LASER_SCAN_STAGE_EXTENSION_MM

	return float(configured_value)


def get_laser_scan_pose_z_delta_from_place_mm(scan_config: Any) -> float:
	configured_value = getattr(
		scan_config,
		'laser_scan_pose_z_delta_from_place_mm',
		None,
	)

	if configured_value is None:
		return LASER_SCAN_Z_OFFSET_MM

	return float(configured_value)

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
