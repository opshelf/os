import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from perpetual_os1.laser_calibration import GapCandidateRegion
from perpetual_os1.laser_scan import ScanBounds


def load_laser_script_module():
	repo_root = Path(__file__).resolve().parents[4]
	script_path = repo_root / 'scripts' / 'laser.py'
	spec = importlib.util.spec_from_file_location('laser_script_test_module', script_path)
	module = importlib.util.module_from_spec(spec)
	assert spec.loader is not None
	spec.loader.exec_module(module)
	return module


def build_config():
	return SimpleNamespace(
		robot=SimpleNamespace(
			grid=SimpleNamespace(
				scan=SimpleNamespace(
					settle_ms=40,
					samples_per_point=3,
					laser_offset_x_mm=51.0,
					laser_offset_y_mm=0.0,
					wall_depth_tolerance_mm=2.0,
					expected_hole_width_mm=26.0,
					expected_hole_height_mm=30.0,
					hole_width_tolerance_mm=10.0,
					hole_height_tolerance_mm=10.0,
					min_vertical_wall_coverage_mm=10.0,
					min_horizontal_wall_coverage_mm=30.0,
				)
			),
			tool=SimpleNamespace(
				parameters=SimpleNamespace(
					ROTATION_FRONT=3.83,
					ROTATION_BACK=185.255,
					model_dump=lambda: {
						'ROTATION_FRONT': 3.83,
						'ROTATION_BACK': 185.255,
					},
				)
			),
		)
	)


def build_analysis(raw_scan_path: str = '/tmp/run/coarse.npy'):
	return SimpleNamespace(
		raw_scan_path=raw_scan_path,
		run_id='run-id',
		shelves=[],
		side=SimpleNamespace(value='left'),
		bounds=SimpleNamespace(x_min=0.0, x_max=10.0, y_min=0.0, y_max=10.0),
		wall_depth_mm=8.0,
	)


def test_main_prints_help_when_no_args(capsys) -> None:
	module = load_laser_script_module()

	result = module.main([])

	assert result == 0
	help_output = capsys.readouterr().out
	assert 'Run tray scan' in help_output
	assert 'IO box' in help_output
	assert 'scouting' in help_output


def test_derive_scans_dir_uses_config_directory(tmp_path: Path) -> None:
	module = load_laser_script_module()
	config_path = tmp_path / 'data' / 'config.json'

	assert module.derive_scans_dir(str(config_path)) == str((config_path.parent / 'calibration').resolve())


def test_main_rejects_scans_dir_option() -> None:
	module = load_laser_script_module()

	with pytest.raises(SystemExit):
		module.main(['--scans-dir', '/tmp'])


def test_main_dispatches_full_scan_to_package_methods(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	module = load_laser_script_module()
	config = build_config()
	analysis = build_analysis()
	run_staged_scan = AsyncMock(return_value=analysis)
	config_path = tmp_path / 'data' / 'config.json'
	expected_scans_dir = str((config_path.parent / 'calibration').resolve())

	async def didRunWithTempRobotConfig(*, config, config_path, didRun, shutdown_action):
		return await didRun(SimpleNamespace(_tool=SimpleNamespace()))

	monkeypatch.setattr(module, 'load_config_file', lambda path: config)
	monkeypatch.setattr(module, 'resolve_grid_path', lambda config: 'storage.json')
	monkeypatch.setattr(module.laser_module, 'load_grid', lambda path: SimpleNamespace())
	monkeypatch.setattr(module.laser_module, 'build_run_parameters', lambda **kwargs: {'module': 'left'})
	monkeypatch.setattr(module.laser_module, 'run_staged_scan', run_staged_scan)
	monkeypatch.setattr(
		module.laser_module,
		'run_with_temp_robot_config',
		AsyncMock(side_effect=didRunWithTempRobotConfig),
	)
	monkeypatch.setattr(module.laser_module, 'persist_run_parameters', lambda **kwargs: '/tmp/run/run_parameters.json')
	monkeypatch.setattr(module, 'print_analysis', lambda analysis: None)

	result = module.main([
		'--module',
		'left',
		'--x',
		'0',
		'10',
		'--y',
		'0',
		'10',
		'--step',
		'1',
		'--config',
		str(config_path),
	])

	assert result == 0
	run_staged_scan.assert_awaited_once()
	assert run_staged_scan.await_args.kwargs['scans_dir'] == expected_scans_dir


def test_main_dispatches_rescan_apply_to_package_methods(monkeypatch: pytest.MonkeyPatch) -> None:
	module = load_laser_script_module()
	config = build_config()
	analysis = build_analysis()
	run_staged_scan = AsyncMock(return_value=analysis)
	apply_single_cell_rescan = Mock()
	apply_analysis = Mock()
	candidate = GapCandidateRegion(
		x_min=10.0,
		x_max=20.0,
		y_min=30.0,
		y_max=40.0,
		wall_depth_mm=8.0,
	)
	detected_cell = SimpleNamespace(tool_x_mm=100.0, tool_y_mm=200.0)

	async def didRunWithTempRobotConfig(*, config, config_path, didRun, shutdown_action):
		return await didRun(SimpleNamespace(_tool=SimpleNamespace()))

	monkeypatch.setattr(module, 'load_config_file', lambda path: config)
	monkeypatch.setattr(module, 'resolve_grid_path', lambda config: 'storage.json')
	monkeypatch.setattr(module.laser_module, 'load_grid', lambda path: SimpleNamespace())
	monkeypatch.setattr(module.laser_module, 'build_single_cell_rescan_candidate', lambda **kwargs: candidate)
	monkeypatch.setattr(
		module.laser_module,
		'build_single_cell_rescan_bounds',
		lambda **kwargs: ScanBounds(x_min=0.0, x_max=50.0, y_min=0.0, y_max=80.0),
	)
	monkeypatch.setattr(module.laser_module, 'is_point_in_bounds', lambda **kwargs: True)
	monkeypatch.setattr(module.laser_module, 'build_run_parameters', lambda **kwargs: {'module': 'left'})
	monkeypatch.setattr(module.laser_module, 'run_staged_scan', run_staged_scan)
	monkeypatch.setattr(
		module.laser_module,
		'run_with_temp_robot_config',
		AsyncMock(side_effect=didRunWithTempRobotConfig),
	)
	monkeypatch.setattr(module.laser_module, 'persist_run_parameters', lambda **kwargs: '/tmp/run/run_parameters.json')
	monkeypatch.setattr(
		module.laser_module,
		'find_detected_cell_for_candidate',
		lambda **kwargs: detected_cell,
	)
	monkeypatch.setattr(module.laser_module, 'apply_single_cell_rescan', apply_single_cell_rescan)
	monkeypatch.setattr(module.laser_module, 'apply_analysis', apply_analysis)
	monkeypatch.setattr(module, 'print_analysis', lambda analysis: None)

	result = module.main(['--module', 'left', '--step', '1', '--rescan-cell', '1:2', '--apply'])

	assert result == 0
	run_staged_scan.assert_awaited_once()
	apply_single_cell_rescan.assert_called_once()
	apply_analysis.assert_not_called()


def test_print_io_box_scout_result_includes_calibration_points(capsys: pytest.CaptureFixture[str]) -> None:
	module = load_laser_script_module()
	result = SimpleNamespace(
		rough_y_min_mm=427.5,
		rough_y_max_mm=1047.5,
		fine_y_min_mm=441.0,
		fine_y_max_mm=795.0,
		measured_x_min_mm=None,
		measured_x_max_mm=74.0,
		derived_x_min_mm=-86.0,
		laser_center_x_mm=-6.0,
		laser_center_y_mm=618.0,
		axis_center_x_mm=45.0,
		axis_center_y_mm=618.0,
		laser_offset_x_mm=51.0,
		laser_offset_y_mm=0.0,
		calibration_low_x_mm=45.0,
		calibration_low_y_mm=476.0,
		calibration_high_x_mm=45.0,
		calibration_high_y_mm=715.0,
		io_box_interior_width_mm=160.0,
		stage_probe_target_mm=120.0,
		stage_probe_stalled=True,
		stage_probe_position_mm=87.2,
		stage_probe_status='no_progress',
		stage_probe_alarm_code=None,
		stage_extension_mm=87.0,
		final_y_refined=False,
	)

	module.print_io_box_scout_result(result)

	output = capsys.readouterr().out
	assert 'io_box_calibration_low_x_mm: 45.00' in output
	assert 'io_box_calibration_low_y_mm: 476.00' in output
	assert 'io_box_calibration_high_x_mm: 45.00' in output
	assert 'io_box_calibration_high_y_mm: 715.00' in output


def test_main_dispatches_io_box_calibration_to_package_method(monkeypatch: pytest.MonkeyPatch) -> None:
	module = load_laser_script_module()
	config = build_config()
	scout_result = SimpleNamespace(
		rough_y_min_mm=1100.0,
		rough_y_max_mm=1200.0,
		fine_y_min_mm=1110.0,
		fine_y_max_mm=1190.0,
		measured_x_min_mm=None,
		measured_x_max_mm=80.0,
		derived_x_min_mm=-40.0,
		laser_center_x_mm=20.0,
		laser_center_y_mm=1150.0,
		axis_center_x_mm=71.0,
		axis_center_y_mm=1150.0,
		laser_offset_x_mm=51.0,
		laser_offset_y_mm=0.0,
		calibration_low_x_mm=71.0,
		calibration_low_y_mm=1145.0,
		calibration_high_x_mm=71.0,
		calibration_high_y_mm=1070.0,
		io_box_interior_width_mm=120.0,
		stage_probe_target_mm=None,
		stage_probe_stalled=None,
		stage_probe_position_mm=None,
		stage_probe_status=None,
		stage_probe_alarm_code=None,
		stage_extension_mm=None,
		final_y_refined=False,
	)
	run_io_box_scout = AsyncMock(return_value=scout_result)

	async def didRunWithTempRobotConfig(*, config, config_path, didRun, shutdown_action):
		return await didRun(SimpleNamespace(_tool=SimpleNamespace(), _io=SimpleNamespace()))

	monkeypatch.setattr(module, 'load_config_file', lambda path: config)
	monkeypatch.setattr(module, 'resolve_grid_path', lambda config: 'storage.json')
	monkeypatch.setattr(module.laser_module, 'load_grid', lambda path: SimpleNamespace())
	monkeypatch.setattr(module.laser_module, 'run_io_box_scout', run_io_box_scout)
	monkeypatch.setattr(
		module.laser_module,
		'run_with_temp_robot_config',
		AsyncMock(side_effect=didRunWithTempRobotConfig),
	)
	monkeypatch.setattr(module, 'print_io_box_scout_result', lambda result: None)

	result = module.main(
		[
			'--calibrate-io-box',
			'--io-box-x-max-mm',
			'140',
		]
	)

	assert result == 0
	run_io_box_scout.assert_awaited_once()
	assert run_io_box_scout.await_args.kwargs['io_box_interior_width_mm'] == pytest.approx(160.0)
	assert run_io_box_scout.await_args.kwargs['start_x_mm'] == pytest.approx(0.0)
	assert run_io_box_scout.await_args.kwargs['x_max_mm'] == pytest.approx(140.0)
	assert run_io_box_scout.await_args.kwargs['laser_offset_x_mm'] == pytest.approx(51.0)


def test_main_dispatches_angle_calibration_to_package_module(monkeypatch: pytest.MonkeyPatch) -> None:
	module = load_laser_script_module()
	config = build_config()
	fit_result = SimpleNamespace(alpha_deg=1.25)
	capture_samples_with_robot = AsyncMock(return_value=(['sample'], {'module': 'left'}))
	search_best_alpha = Mock(return_value=fit_result)
	print_fit_result = Mock()

	monkeypatch.setattr(module, 'load_config_file', lambda path: config)
	monkeypatch.setattr(module, 'resolve_grid_path', lambda config: 'storage.json')
	monkeypatch.setattr(module.laser_module, 'load_grid', lambda path: SimpleNamespace())
	monkeypatch.setattr(module.angle_module, 'capture_samples_with_robot', capture_samples_with_robot)
	monkeypatch.setattr(module.angle_module, 'search_best_alpha', search_best_alpha)
	monkeypatch.setattr(module.angle_module, 'print_fit_result', print_fit_result)

	result = module.main(
		[
			'--calibrate-angle',
			'--module',
			'left',
			'--x-range',
			'0',
			'10',
			'1',
			'--y-mm',
			'100',
			'--z-offset-range',
			'0',
			'0',
			'1',
			'--rotation-range',
			'0',
			'0',
			'1',
		]
	)

	assert result == 0
	capture_samples_with_robot.assert_awaited_once()
	search_best_alpha.assert_called_once()
	print_fit_result.assert_called_once_with(fit_result)
