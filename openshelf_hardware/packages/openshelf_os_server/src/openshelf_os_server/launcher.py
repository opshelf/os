from __future__ import annotations

from openshelf_os.operating_mode import OperatingMode
from openshelf_os.runtime_state import RuntimeStatePaths, ensure_mode_workspace


def _migrate_legacy_production_config() -> None:
    active = RuntimeStatePaths.current()
    production = RuntimeStatePaths.for_mode(OperatingMode.production, active.data_dir)
    legacy_settings = active.data_dir / "settings.json"

    if production.config_path.exists() or legacy_settings.exists() is False:
        return

    from openshelf_os_server.settings import _migrate_legacy_settings  # noqa: PLC0415

    _migrate_legacy_settings(legacy_settings, production.config_path)


def start() -> None:
    _migrate_legacy_production_config()
    ensure_mode_workspace()

    from openshelf_os_server.main import start as start_server  # noqa: PLC0415

    start_server()
