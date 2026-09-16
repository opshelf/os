import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openshelf_os.laser_calibration import GapCandidateRegion
from openshelf_os.laser_calibration.scan import ScanBounds
from openshelf_os.storage import StorageModuleSide


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
				path='storage.json',
				scan=SimpleNamespace(
					laser_sample_settle_ms=40,
					laser_samples_per_point=3,
					laser_offset_x_mm=51.0,
					laser_offset_y_mm=0.0,
					left_xy_offset=SimpleNamespace(x_mm=10.0, y_mm=20.0),
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
				),
				motors=SimpleNamespace(
					by_name=lambda name: SimpleNamespace(lim=360.0),
				),
			),
		)
	)


def build_analysis(raw_scan_path: str = '/tmp/run/coarse.npy'):
	return SimpleNamespace(
		raw_scan_path=raw_scan_path,
		run_id='run-id',
		shelves=[],
		side=StorageModuleSide.LEFT,
		bounds=SimpleNamespace(x_min=0.0, x_max=10.0, y_min=0.0, y_max=10.0),
		wall_depth_mm=8.0,
	)


def test_main_prints_help_when_no_args(capsys) -> None:
	module = load_laser_script_module()

	result = module.main([])

	assert result == 0
	help_output = capsys.readouterr().out
	assert 'Run tray scan' in help_output
	assert 'single-cell rescan' in help_output
	assert 'validation' in help_output


def test_derive_scans_dir_uses_config_directory(tmp_path: Path) -> None:
	module = load_laser_script_module()
	config_path = tmp_path / 'data' / 'config.json'

	assert module.derive_scans_dir(str(config_path)) == str((config_path.parent / 'calibration').resolve())


def test_selected_config_paths_preserve_current_config_override_workspace(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	data_dir = tmp_path / 'data'
	config_path = tmp_path / 'custom' / 'config.json'
	monkeypatch.setenv('OPENSHELF_DATA_DIR', str(data_dir))
	monkeypatch.setenv('OPENSHELF_CONFIG_PATH', str(config_path))
	monkeypatch.setenv('OPERATING_MODE', 'development')
	module = load_laser_script_module()

	paths = module.paths_for_selected_config(str(config_path))

	assert paths.config_path == config_path.resolve()
	assert paths.database_path == (data_dir / 'base.development.db').resolve()
	assert paths.calibration_dir == (data_dir / 'calibration/development').resolve()
	assert paths.mode.value == 'development'


def test_run_scan_explicit_config_uses_active_mode_workspace_and_selected_storage(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	data_dir = tmp_path / 'data'
	active_config_path = data_dir / 'config.development.json'
	selected_config_path = tmp_path / 'custom' / 'config.json'
	monkeypatch.setenv('OPENSHELF_DATA_DIR', str(data_dir))
	monkeypatch.setenv('OPENSHELF_CONFIG_PATH', str(active_config_path))
	monkeypatch.setenv('OPERATING_MODE', 'development')
	module = load_laser_script_module()
	config = build_config()
	config.robot.grid.path = 'grids/storage.json'
	workspace = Mock()
	load_grid = Mock(return_value=SimpleNamespace())
	analysis = build_analysis()
	analysis.metadata_path = str(tmp_path / 'run' / 'metadata.json')

	monkeypatch.setattr(module, 'ensure_mode_workspace', workspace)
	monkeypatch.setattr(module, 'load_config_file', lambda path: config)
	monkeypatch.setattr(module.laser_module, 'load_grid', load_grid)
	monkeypatch.setattr(module.laser_module, 'load_staged_scan_analysis', lambda *args, **kwargs: analysis)
	monkeypatch.setattr(module, 'load_run_parameters_for_analysis', lambda analysis: {})
	monkeypatch.setattr(module, 'print_analysis', lambda analysis: None)

	args = module.build_parser().parse_args([
		'--from-run',
		str(tmp_path / 'run'),
		'--config',
		str(selected_config_path),
	])

	assert asyncio.run(module.run_scan(args)) == 0

	workspace.assert_called_once()
	workspace_paths = workspace.call_args.args[0]
	assert workspace_paths.config_path == selected_config_path.resolve()
	assert workspace_paths.database_path == (data_dir / 'base.development.db').resolve()
	assert workspace_paths.calibration_dir == (data_dir / 'calibration/development').resolve()
	assert workspace_paths.mode.value == 'development'
	assert args.scans_dir == str((data_dir / 'calibration/development').resolve())
	load_grid.assert_called_once_with((selected_config_path.parent / 'grids/storage.development.json').resolve())


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
	expected_scans_dir = str(module.paths_for_selected_config(str(config_path)).calibration_dir)

	async def didRunWithTempRobotConfig(*, config, config_path, didRun, shutdown_action):
		return await didRun(SimpleNamespace(_tool=SimpleNamespace()))

	monkeypatch.setattr(module, 'load_config_file', lambda path: config)
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

def test_from_run_apply_uses_single_shelf_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	module = load_laser_script_module()
	config = build_config()
	analysis = build_analysis(str(tmp_path / 'run' / 'coarse.npy'))
	analysis.metadata_path = str(tmp_path / 'run' / 'metadata.json')
	apply_single_shelf_analysis = Mock()
	apply_analysis = Mock()

	monkeypatch.setattr(module, 'load_config_file', lambda path: config)
	monkeypatch.setattr(module.laser_module, 'load_grid', lambda path: SimpleNamespace())
	monkeypatch.setattr(module.laser_module, 'load_staged_scan_analysis', lambda *args, **kwargs: analysis)
	monkeypatch.setattr(
		module,
		'load_run_parameters_for_analysis',
		lambda analysis: {
			'auto_calibration_scope': {
				'mode': 'shelf',
				'side': 'left',
				'shelf_idx': 2,
			},
		},
	)
	monkeypatch.setattr(module.laser_module, 'apply_single_shelf_analysis', apply_single_shelf_analysis)
	monkeypatch.setattr(module.laser_module, 'apply_analysis', apply_analysis)
	monkeypatch.setattr(module, 'print_analysis', lambda analysis: None)

	result = module.main(['--from-run', str(tmp_path / 'run'), '--apply'])

	assert result == 0
	apply_single_shelf_analysis.assert_called_once()
	assert apply_single_shelf_analysis.call_args.kwargs['target_shelf_idx'] == 2
	apply_analysis.assert_not_called()


def test_from_run_apply_uses_single_cell_rescan(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	module = load_laser_script_module()
	config = build_config()
	analysis = build_analysis(str(tmp_path / 'run' / 'coarse.npy'))
	analysis.metadata_path = str(tmp_path / 'run' / 'metadata.json')
	candidate = GapCandidateRegion(x_min=10.0, x_max=20.0, y_min=30.0, y_max=40.0, wall_depth_mm=8.0)
	detected_cell = SimpleNamespace(tool_x_mm=100.0, tool_y_mm=200.0)
	apply_single_cell_rescan = Mock()

	monkeypatch.setattr(module, 'load_config_file', lambda path: config)
	monkeypatch.setattr(module.laser_module, 'load_grid', lambda path: SimpleNamespace())
	monkeypatch.setattr(module.laser_module, 'load_staged_scan_analysis', lambda *args, **kwargs: analysis)
	monkeypatch.setattr(
		module,
		'load_run_parameters_for_analysis',
		lambda analysis: {
			'single_cell_rescan': {
				'shelf_idx': 1,
				'cell_idx': 2,
				'candidate': {
					'x_min': candidate.x_min,
					'x_max': candidate.x_max,
					'y_min': candidate.y_min,
					'y_max': candidate.y_max,
					'wall_depth_mm': candidate.wall_depth_mm,
				},
			},
		},
	)
	monkeypatch.setattr(module.laser_module, 'find_detected_cell_for_candidate', lambda **kwargs: detected_cell)
	monkeypatch.setattr(module.laser_module, 'apply_single_cell_rescan', apply_single_cell_rescan)
	monkeypatch.setattr(module, 'print_analysis', lambda analysis: None)

	result = module.main(['--from-run', str(tmp_path / 'run'), '--apply'])

	assert result == 0
	apply_single_cell_rescan.assert_called_once()
	call_kwargs = apply_single_cell_rescan.call_args.kwargs
	assert call_kwargs['target'].shelf_idx == 1
	assert call_kwargs['target'].cell_idx == 2
	assert call_kwargs['side'] == StorageModuleSide.LEFT
	assert call_kwargs['detected_cell'] is detected_cell
