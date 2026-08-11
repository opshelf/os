from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import ModuleType, SimpleNamespace


PROBE_PATH = Path(__file__).parents[1] / "z_wall_probe.py"


def load_probe_module() -> ModuleType:
	spec = importlib.util.spec_from_file_location("z_wall_probe_test", PROBE_PATH)
	assert spec is not None
	assert spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def grid_config(storage_path: str) -> SimpleNamespace:
	return SimpleNamespace(robot=SimpleNamespace(grid=SimpleNamespace(path=storage_path)))


def test_default_probe_paths_follow_config_override_and_initialize_its_workspace(
	tmp_path: Path, monkeypatch
) -> None:
	probe = load_probe_module()
	data_dir = tmp_path / "data"
	custom_dir = tmp_path / "custom"
	data_dir.mkdir()
	custom_dir.mkdir()
	(data_dir / ".mode").write_text("development", encoding="utf-8")
	(data_dir / "config.json").write_text(
		json.dumps({"robot": {"grid": {"path": "storage.json"}}}), encoding="utf-8"
	)
	(data_dir / "storage.json").write_text('{"workspace": "production"}', encoding="utf-8")
	with sqlite3.connect(data_dir / "base.db") as connection:
		connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
	custom_config = custom_dir / "active.json"
	custom_config.write_text(
		json.dumps({"robot": {"grid": {"path": "grids/active.json"}}}), encoding="utf-8"
	)
	monkeypatch.setattr(probe, "DATA_DIR", data_dir)
	monkeypatch.setenv("OPENSHELF_CONFIG_PATH", str(custom_config))
	loaded_config = grid_config("grids/active.json")
	monkeypatch.setattr(probe, "load_config_file", lambda path: loaded_config)

	args = probe.build_parser().parse_args(
		["--module", "left", "--z-start-mm", "5", "--z-target-mm", "180"]
	)
	config, config_path = probe.resolve_probe_runtime(args)

	assert config is loaded_config
	assert config_path == custom_config.resolve()
	assert args.storage == str((custom_dir / "grids/active.development.json").resolve())
	assert (custom_dir / "grids/active.development.json").read_text(encoding="utf-8") == '{"workspace": "production"}'
	assert (data_dir / "base.development.db").exists()
	assert (data_dir / "config.development.json").exists() is False


def test_explicit_probe_config_and_storage_override_runtime_defaults(tmp_path: Path, monkeypatch) -> None:
	probe = load_probe_module()
	data_dir = tmp_path / "data"
	data_dir.mkdir()
	(data_dir / ".mode").write_text("development", encoding="utf-8")
	selected_config = tmp_path / "selected.json"
	selected_config.write_text('{"robot": {"grid": {"path": "ignored.json"}}}', encoding="utf-8")
	explicit_storage = tmp_path / "explicit-storage.json"
	monkeypatch.setattr(probe, "DATA_DIR", data_dir)
	loaded_config = grid_config("ignored.json")
	monkeypatch.setattr(probe, "load_config_file", lambda path: loaded_config)

	args = probe.build_parser().parse_args(
		[
			"--module",
			"left",
			"--z-start-mm",
			"5",
			"--z-target-mm",
			"180",
			"--config",
			str(selected_config),
			"--storage",
			str(explicit_storage),
		]
	)
	config, config_path = probe.resolve_probe_runtime(args)

	assert config is loaded_config
	assert config_path == selected_config.resolve()
	assert args.storage == str(explicit_storage.resolve())
