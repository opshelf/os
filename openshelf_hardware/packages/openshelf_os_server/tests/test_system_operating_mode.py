import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from openshelf_os.common import OperatingMode
from openshelf_os_server import settings as settings_module
from openshelf_os_server.routers.robot import inject_mode_field
from openshelf_os_server.routers.system import _restart_process_id
from openshelf_os_server.settings import DEVELOPER_SYSTEM_USER_EMAIL, get_operating_mode, clear_config_cache


class TestOperatingModeEnum:
	def test_values(self) -> None:
		assert OperatingMode.demo.value == "demo"
		assert OperatingMode.development.value == "development"
		assert OperatingMode.mock.value == "mock"
		assert OperatingMode.production.value == "production"


class TestGetOperatingMode:
	def setup_method(self) -> None:
		clear_config_cache()

	def teardown_method(self) -> None:
		clear_config_cache()

	def _no_mode_file(self):
		fake = Path("/nonexistent/.mode")
		return patch("openshelf_os_server.settings.MODE_FILE", fake)

	def test_defaults_to_development(self) -> None:
		with patch.dict(os.environ, {}, clear=False), self._no_mode_file():
			os.environ.pop("OPERATING_MODE", None)
			clear_config_cache()
			assert get_operating_mode() == OperatingMode.development

	@pytest.mark.parametrize("mode", ["demo", "production", "mock", "development"])
	def test_reads_mode_from_env(self, mode: str) -> None:
		with patch.dict(os.environ, {"OPERATING_MODE": mode}), self._no_mode_file():
			clear_config_cache()
			assert get_operating_mode() == OperatingMode(mode)

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

	def test_case_insensitive(self) -> None:
		with patch.dict(os.environ, {"OPERATING_MODE": "DEMO"}), self._no_mode_file():
			clear_config_cache()
			assert get_operating_mode() == OperatingMode.demo

	def test_strips_whitespace(self) -> None:
		with patch.dict(os.environ, {"OPERATING_MODE": "  production  "}), self._no_mode_file():
			clear_config_cache()
			assert get_operating_mode() == OperatingMode.production

	def test_invalid_env_falls_back_to_development(self) -> None:
		with patch.dict(os.environ, {"OPERATING_MODE": "staging"}), self._no_mode_file():
			clear_config_cache()
			assert get_operating_mode() == OperatingMode.development

	def test_reads_current_shared_source(self) -> None:
		with patch.dict(os.environ, {"OPERATING_MODE": "demo"}), self._no_mode_file():
			first = get_operating_mode()

		with patch.dict(os.environ, {"OPERATING_MODE": "production"}), self._no_mode_file():
			second = get_operating_mode()

		assert first == OperatingMode.demo
		assert second == OperatingMode.production

	def test_clear_cache_resets(self) -> None:
		with patch.dict(os.environ, {"OPERATING_MODE": "demo"}), self._no_mode_file():
			clear_config_cache()
			assert get_operating_mode() == OperatingMode.demo

		with patch.dict(os.environ, {"OPERATING_MODE": "production"}), self._no_mode_file():
			clear_config_cache()
			assert get_operating_mode() == OperatingMode.production


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

	# The dev fallback is forced to the identity this build seeds; a stale value in
	# config.json (admin@opshelf.com on devices flashed before this) would otherwise
	# leave every unauthenticated request answering 503.
	def test_development_mode_forces_seeded_dev_auto_user(self, monkeypatch) -> None:
		monkeypatch.setenv("ENV", "production")
		monkeypatch.setattr(
			settings_module,
			"get_operating_mode",
			lambda: OperatingMode.development,
		)
		data = {"auth": {"dev_mode": False, "require_auth": True, "dev_auto_user": "admin@opshelf.com"}}

		result = settings_module._apply_env_overrides(data)

		assert result["auth"]["dev_auto_user"] == DEVELOPER_SYSTEM_USER_EMAIL

	def test_test_env_keeps_test_auth_defaults(self, monkeypatch) -> None:
		monkeypatch.setenv("ENV", "test")
		monkeypatch.setattr(
			settings_module,
			"get_operating_mode",
			lambda: OperatingMode.development,
		)
		data = {"auth": {"dev_mode": True, "require_auth": False}}

		result = settings_module._apply_env_overrides(data)

		assert result["auth"]["dev_mode"] is False
		assert result["auth"]["require_auth"] is True


class TestInjectModeField:
	def test_adds_mode_to_payload(self) -> None:
		payload = json.dumps({
			"robot_status": {"state": "idle", "storage": {}},
			"current_command": None,
			"queue_length": 3,
		})

		with patch("openshelf_os_server.routers.robot.get_operating_mode", return_value=OperatingMode.demo):
			result = json.loads(inject_mode_field(payload))

		assert result["mode"] == "demo"
		assert result["robot_status"] == {"state": "idle", "storage": {}}
		assert result["current_command"] is None
		assert result["queue_length"] == 3

	def test_overwrites_existing_mode(self) -> None:
		payload = json.dumps({"mode": "old_value", "data": 1})

		with patch("openshelf_os_server.routers.robot.get_operating_mode", return_value=OperatingMode.production):
			result = json.loads(inject_mode_field(payload))

		assert result["mode"] == "production"
		assert result["data"] == 1

	def test_empty_payload(self) -> None:
		payload = json.dumps({})

		with patch("openshelf_os_server.routers.robot.get_operating_mode", return_value=OperatingMode.development):
			result = json.loads(inject_mode_field(payload))

		assert result == {"mode": "development"}


class TestSystemRestartHelpers:
	def test_restart_process_id_targets_container_init_in_docker(self) -> None:
		with patch("openshelf_os_server.routers.system.os.path.exists", return_value=True):
			assert _restart_process_id() == 1

	def test_restart_process_id_targets_current_process_outside_docker(self) -> None:
		with patch("openshelf_os_server.routers.system.os.path.exists", return_value=False):
			assert _restart_process_id() == os.getpid()


class TestSystemModeEndpoints:
	@pytest.fixture
	async def client(self, seeded_db, auth_headers) -> httpx.AsyncClient:
		from httpx import AsyncClient, ASGITransport
		from openshelf_os_server.main import app

		transport = ASGITransport(app=app)
		async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True, headers=auth_headers) as ac:
			yield ac

	@pytest.mark.anyio
	async def test_get_mode(self, client: httpx.AsyncClient) -> None:
		with patch("openshelf_os_server.routers.system.get_operating_mode", return_value=OperatingMode.demo):
			response = await client.get("/api/system/mode")

		assert response.status_code == 200
		assert response.json()["mode"] == "demo"

	@pytest.mark.anyio
	async def test_put_mode_is_not_supported(self, client: httpx.AsyncClient) -> None:
		response = await client.put("/api/system/mode", json={"mode": "production"})

		assert response.status_code == 405

	@pytest.mark.anyio
	async def test_get_mode_unauthenticated(self, seeded_db) -> None:
		from httpx import AsyncClient, ASGITransport
		from openshelf_os_server.main import app

		transport = ASGITransport(app=app)
		async with AsyncClient(transport=transport, base_url="http://test") as ac:
			response = await ac.get("/api/system/mode")

		assert response.status_code == 401


class TestModeWorkspaceActivation:
	@pytest.mark.anyio
	@pytest.mark.parametrize("recovered_state", ["database", "storage"])
	async def test_recovered_demo_core_state_reseeds_once(
		self, tmp_path, recovered_state: str
	) -> None:
		from openshelf_os.runtime_state import RuntimeStatePaths, ensure_mode_workspace
		from openshelf_os_server import main

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
	async def test_existing_demo_mode_seeds_new_demo_workspace(self, tmp_path) -> None:
		from openshelf_os_server import main

		last_mode = tmp_path / ".last_mode"
		last_mode.write_text("demo", encoding="utf-8")
		demo_seeded = tmp_path / ".demo_workspace_seeded"

		with (
			patch.object(main, "LAST_MODE_FILE", last_mode),
			patch.object(main, "DEMO_SEEDED_FILE", demo_seeded),
			patch.object(main, "get_operating_mode", return_value=OperatingMode.demo),
			patch.object(main, "_run_demo_seed", return_value=True) as run_demo_seed,
		):
			await main._apply_mode_transition()

		run_demo_seed.assert_called_once_with()
		assert demo_seeded.read_text(encoding="utf-8") == "seeded\n"

	@pytest.mark.anyio
	async def test_returning_to_seeded_demo_workspace_does_not_reseed(self, tmp_path) -> None:
		from openshelf_os_server import main

		last_mode = tmp_path / ".last_mode"
		last_mode.write_text("production", encoding="utf-8")
		demo_seeded = tmp_path / ".demo_workspace_seeded"
		demo_seeded.write_text("seeded\n", encoding="utf-8")

		with (
			patch.object(main, "LAST_MODE_FILE", last_mode),
			patch.object(main, "DEMO_SEEDED_FILE", demo_seeded),
			patch.object(main, "get_operating_mode", return_value=OperatingMode.demo),
			patch.object(main, "_run_demo_seed") as run_demo_seed,
		):
			await main._apply_mode_transition()

		run_demo_seed.assert_not_called()
		assert last_mode.read_text(encoding="utf-8") == "demo"

	@pytest.mark.anyio
	async def test_same_production_mode_has_no_activation_side_effects(self, tmp_path) -> None:
		from openshelf_os_server import main

		last_mode = tmp_path / ".last_mode"
		last_mode.write_text("production", encoding="utf-8")

		with (
			patch.object(main, "LAST_MODE_FILE", last_mode),
			patch.object(main, "get_operating_mode", return_value=OperatingMode.production),
			patch.object(main, "_run_demo_seed") as run_demo_seed,
		):
			await main._apply_mode_transition()

		run_demo_seed.assert_not_called()


class TestHealthEndpoint:
	@pytest.fixture
	async def client(self, seeded_db, auth_headers) -> httpx.AsyncClient:
		from httpx import AsyncClient, ASGITransport
		from openshelf_os_server.main import app

		transport = ASGITransport(app=app)
		async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True, headers=auth_headers) as ac:
			yield ac

	@pytest.mark.anyio
	async def test_health_includes_mode(self, client: httpx.AsyncClient) -> None:
		with patch("openshelf_os_server.main.get_operating_mode", return_value=OperatingMode.production):
			response = await client.get("/api/health")

		assert response.status_code == 200
		body = response.json()
		assert body["status"] == "ok"
		assert body["mode"] == "production"


class TestSystemInfoEndpoint:
	@pytest.fixture
	async def client(self, seeded_db, auth_headers) -> httpx.AsyncClient:
		from httpx import AsyncClient, ASGITransport
		from openshelf_os_server.main import app

		transport = ASGITransport(app=app)
		async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True, headers=auth_headers) as ac:
			yield ac

	@pytest.mark.anyio
	async def test_system_info_reports_device_alias(self, client: httpx.AsyncClient) -> None:
		with patch("openshelf_os_server.main.get_device_alias", return_value="lb4a-3"):
			response = await client.get("/api/system/info")

		assert response.status_code == 200
		assert response.json()["device_alias"] == "lb4a-3"

	@pytest.mark.anyio
	async def test_system_info_alias_is_null_without_alias(self, client: httpx.AsyncClient) -> None:
		with patch("openshelf_os_server.main.get_device_alias", return_value=""):
			response = await client.get("/api/system/info")

		assert response.status_code == 200
		assert response.json()["device_alias"] is None
