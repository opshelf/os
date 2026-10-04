import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from openshelf_os.common import OperatingMode
from openshelf_os.runtime_state import RuntimeStatePaths, ensure_mode_workspace
from openshelf_os_server import main
from openshelf_os_server import settings as settings_module
from openshelf_os_server.settings import get_operating_mode, clear_config_cache


class TestGetOperatingMode:
	def setup_method(self) -> None:
		clear_config_cache()

	def teardown_method(self) -> None:
		clear_config_cache()

	def test_file_takes_precedence_over_env(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			mode_file = Path(tmpdir) / ".mode"
			mode_file.write_text("production")

			with (
				patch.dict(os.environ, {"OPERATING_MODE": "demo"}),
				patch("openshelf_os_server.settings.MODE_FILE", mode_file),
			):
				clear_config_cache()
				assert get_operating_mode() == OperatingMode.production

	def test_file_invalid_falls_back_to_env(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			mode_file = Path(tmpdir) / ".mode"
			mode_file.write_text("staging")

			with (
				patch.dict(os.environ, {"OPERATING_MODE": "demo"}),
				patch("openshelf_os_server.settings.MODE_FILE", mode_file),
			):
				clear_config_cache()
				assert get_operating_mode() == OperatingMode.demo


class TestOperatingModeEnvironmentDefaults:
	@pytest.mark.parametrize("mode", [OperatingMode.demo, OperatingMode.mock, OperatingMode.production])
	def test_production_like_modes_override_stale_development_env(
		self,
		monkeypatch,
		mode: OperatingMode,
	) -> None:
		monkeypatch.setenv("ENV", "dev")
		monkeypatch.setattr(settings_module, "get_operating_mode", lambda: mode)
		data = {"auth": {"dev_mode": False, "require_auth": True}}

		result = settings_module._apply_env_overrides(data)

		assert result["auth"]["dev_mode"] is False
		assert result["auth"]["require_auth"] is True

	def test_development_mode_overrides_stale_production_env(self, monkeypatch) -> None:
		monkeypatch.setenv("ENV", "production")
		monkeypatch.setattr(
			settings_module,
			"get_operating_mode",
			lambda: OperatingMode.development,
		)
		data = {"auth": {"dev_mode": False, "require_auth": True}}

		result = settings_module._apply_env_overrides(data)

		assert result["auth"]["dev_mode"] is True
		assert result["auth"]["require_auth"] is False


class TestModeWorkspaceActivation:
	@pytest.mark.anyio
	@pytest.mark.parametrize("recovered_state", ["database", "storage"])
	async def test_recovered_demo_core_state_reseeds_once(
		self, tmp_path, recovered_state: str
	) -> None:
		data_dir = tmp_path / "data"
		data_dir.mkdir()
		(data_dir / "config.json").write_text(
			'{"robot": {"grid": {"path": "storage.json"}}}', encoding="utf-8"
		)
		(data_dir / "storage.json").write_text('{"workspace": "production"}', encoding="utf-8")
		production_db = data_dir / "base.db"
		with sqlite3.connect(production_db) as connection:
			connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
			demo = RuntimeStatePaths.for_mode(OperatingMode.demo, data_dir)
		ensure_mode_workspace(demo)

		last_mode = data_dir / ".last_mode"
		last_mode.write_text("demo", encoding="utf-8")
		demo_seeded = data_dir / ".demo_workspace_seeded"
		demo_seeded.write_text("seeded\n", encoding="utf-8")
		if recovered_state == "database":
			demo.database_path.unlink()
		else:
			(data_dir / "storage.demo.json").unlink()

		ensure_mode_workspace(demo)
		assert demo_seeded.exists() is False

		with (
			patch.object(main, "LAST_MODE_FILE", last_mode),
			patch.object(main, "DEMO_SEEDED_FILE", demo_seeded),
			patch.object(main, "get_operating_mode", return_value=OperatingMode.demo),
			patch.object(main, "_run_demo_seed", return_value=True) as run_demo_seed,
		):
			await main._apply_mode_transition()
			await main._apply_mode_transition()

		run_demo_seed.assert_called_once_with()

	@pytest.mark.anyio
	async def test_same_production_mode_has_no_activation_side_effects(self, tmp_path) -> None:
		last_mode = tmp_path / ".last_mode"
		last_mode.write_text("production", encoding="utf-8")

		with (
			patch.object(main, "LAST_MODE_FILE", last_mode),
			patch.object(main, "get_operating_mode", return_value=OperatingMode.production),
			patch.object(main, "_run_demo_seed") as run_demo_seed,
		):
			await main._apply_mode_transition()

		run_demo_seed.assert_not_called()
