
import pytest

from openshelf_os.common import OperatingMode
from openshelf_os_server import settings as settings_module
from openshelf_os_server.settings import clear_config_cache


class TestGetOperatingMode:
	def setup_method(self) -> None:
		clear_config_cache()

	def teardown_method(self) -> None:
		clear_config_cache()


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
