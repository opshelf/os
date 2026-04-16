import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from perpetual_os1.common import OperatingMode
from perpetual_os1_server.routers.robot import inject_mode_field
from perpetual_os1_server.settings import get_operating_mode, clear_config_cache


class TestOperatingModeEnum:
	def test_values(self) -> None:
		assert OperatingMode.demo.value == "demo"
		assert OperatingMode.development.value == "development"
		assert OperatingMode.production.value == "production"

	def test_is_str_enum(self) -> None:
		assert isinstance(OperatingMode.demo, str)
		assert OperatingMode.production == "production"

	def test_all_members(self) -> None:
		assert set(OperatingMode) == {
			OperatingMode.demo,
			OperatingMode.development,
			OperatingMode.production,
		}


class TestGetOperatingMode:
	def setup_method(self) -> None:
		clear_config_cache()

	def teardown_method(self) -> None:
		clear_config_cache()

	def _no_mode_file(self):
		fake = Path("/nonexistent/.mode")
		return patch("perpetual_os1_server.settings.MODE_FILE", fake)

	def test_defaults_to_development(self) -> None:
		with patch.dict(os.environ, {}, clear=False), self._no_mode_file():
			os.environ.pop("OPERATING_MODE", None)
			clear_config_cache()
			assert get_operating_mode() == OperatingMode.development

	def test_reads_demo_from_env(self) -> None:
		with patch.dict(os.environ, {"OPERATING_MODE": "demo"}), self._no_mode_file():
			clear_config_cache()
			assert get_operating_mode() == OperatingMode.demo

	def test_reads_production_from_env(self) -> None:
		with patch.dict(os.environ, {"OPERATING_MODE": "production"}), self._no_mode_file():
			clear_config_cache()
			assert get_operating_mode() == OperatingMode.production

	def test_reads_development_from_env(self) -> None:
		with patch.dict(os.environ, {"OPERATING_MODE": "development"}), self._no_mode_file():
			clear_config_cache()
			assert get_operating_mode() == OperatingMode.development

	def test_file_takes_precedence_over_env(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			mode_file = Path(tmpdir) / ".mode"
			mode_file.write_text("production")

			with (
				patch.dict(os.environ, {"OPERATING_MODE": "demo"}),
				patch("perpetual_os1_server.settings.MODE_FILE", mode_file),
			):
				clear_config_cache()
				assert get_operating_mode() == OperatingMode.production

	def test_file_invalid_falls_back_to_env(self) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			mode_file = Path(tmpdir) / ".mode"
			mode_file.write_text("staging")

			with (
				patch.dict(os.environ, {"OPERATING_MODE": "demo"}),
				patch("perpetual_os1_server.settings.MODE_FILE", mode_file),
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

	def test_caches_result(self) -> None:
		with patch.dict(os.environ, {"OPERATING_MODE": "demo"}), self._no_mode_file():
			clear_config_cache()
			first = get_operating_mode()

		with patch.dict(os.environ, {"OPERATING_MODE": "production"}):
			second = get_operating_mode()

		assert first == OperatingMode.demo
		assert second == OperatingMode.demo

	def test_clear_cache_resets(self) -> None:
		with patch.dict(os.environ, {"OPERATING_MODE": "demo"}), self._no_mode_file():
			clear_config_cache()
			assert get_operating_mode() == OperatingMode.demo

		with patch.dict(os.environ, {"OPERATING_MODE": "production"}), self._no_mode_file():
			clear_config_cache()
			assert get_operating_mode() == OperatingMode.production


class TestInjectModeField:
	def test_adds_mode_to_payload(self) -> None:
		payload = json.dumps({"robot_status": {"state": "idle"}})

		with patch("perpetual_os1_server.routers.robot.get_operating_mode", return_value=OperatingMode.demo):
			result = json.loads(inject_mode_field(payload))

		assert result["mode"] == "demo"
		assert result["robot_status"]["state"] == "idle"

	def test_overwrites_existing_mode(self) -> None:
		payload = json.dumps({"mode": "old_value", "data": 1})

		with patch("perpetual_os1_server.routers.robot.get_operating_mode", return_value=OperatingMode.production):
			result = json.loads(inject_mode_field(payload))

		assert result["mode"] == "production"
		assert result["data"] == 1

	def test_empty_payload(self) -> None:
		payload = json.dumps({})

		with patch("perpetual_os1_server.routers.robot.get_operating_mode", return_value=OperatingMode.development):
			result = json.loads(inject_mode_field(payload))

		assert result == {"mode": "development"}

	def test_preserves_all_fields(self) -> None:
		payload = json.dumps({
			"robot_status": {"storage": {}},
			"current_command": None,
			"queue_length": 3,
		})

		with patch("perpetual_os1_server.routers.robot.get_operating_mode", return_value=OperatingMode.demo):
			result = json.loads(inject_mode_field(payload))

		assert result["mode"] == "demo"
		assert result["robot_status"] == {"storage": {}}
		assert result["current_command"] is None
		assert result["queue_length"] == 3


class TestSystemModeEndpoints:
	@pytest.fixture
	async def client(self, seeded_db, auth_headers) -> httpx.AsyncClient:
		from httpx import AsyncClient, ASGITransport
		from perpetual_os1_server.main import app

		transport = ASGITransport(app=app)
		async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True, headers=auth_headers) as ac:
			yield ac

	@pytest.mark.anyio
	async def test_get_mode(self, client: httpx.AsyncClient) -> None:
		with patch("perpetual_os1_server.routers.system.get_operating_mode", return_value=OperatingMode.demo):
			response = await client.get("/api/system/mode")

		assert response.status_code == 200
		assert response.json()["mode"] == "demo"

	@pytest.mark.anyio
	async def test_put_mode_writes_file_and_schedules_restart(self, client: httpx.AsyncClient) -> None:
		with tempfile.TemporaryDirectory() as tmpdir:
			mode_file = Path(tmpdir) / ".mode"

			with (
				patch("perpetual_os1_server.routers.system.get_operating_mode", return_value=OperatingMode.development),
				patch("perpetual_os1_server.routers.system.MODE_FILE", mode_file),
				patch("perpetual_os1_server.routers.system.asyncio") as mock_asyncio,
			):
				response = await client.put("/api/system/mode", json={"mode": "production"})

			assert response.status_code == 200
			body = response.json()
			assert body["mode"] == "production"
			assert "restart" in body["message"].lower()
			assert mode_file.read_text() == "production"
			mock_asyncio.get_running_loop.return_value.call_later.assert_called_once()

	@pytest.mark.anyio
	async def test_put_mode_same_returns_409(self, client: httpx.AsyncClient) -> None:
		with patch("perpetual_os1_server.routers.system.get_operating_mode", return_value=OperatingMode.demo):
			response = await client.put("/api/system/mode", json={"mode": "demo"})

		assert response.status_code == 409

	@pytest.mark.anyio
	async def test_put_mode_invalid_returns_422(self, client: httpx.AsyncClient) -> None:
		with patch("perpetual_os1_server.routers.system.get_operating_mode", return_value=OperatingMode.demo):
			response = await client.put("/api/system/mode", json={"mode": "staging"})

		assert response.status_code == 400

	@pytest.mark.anyio
	async def test_get_mode_unauthenticated(self, seeded_db) -> None:
		from httpx import AsyncClient, ASGITransport
		from perpetual_os1_server.main import app

		transport = ASGITransport(app=app)
		async with AsyncClient(transport=transport, base_url="http://test") as ac:
			response = await ac.get("/api/system/mode")

		assert response.status_code == 401


class TestHealthEndpoint:
	@pytest.fixture
	async def client(self, seeded_db, auth_headers) -> httpx.AsyncClient:
		from httpx import AsyncClient, ASGITransport
		from perpetual_os1_server.main import app

		transport = ASGITransport(app=app)
		async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True, headers=auth_headers) as ac:
			yield ac

	@pytest.mark.anyio
	async def test_health_includes_mode(self, client: httpx.AsyncClient) -> None:
		with patch("perpetual_os1_server.main.get_operating_mode", return_value=OperatingMode.production):
			response = await client.get("/api/health")

		assert response.status_code == 200
		body = response.json()
		assert body["status"] == "ok"
		assert body["mode"] == "production"
