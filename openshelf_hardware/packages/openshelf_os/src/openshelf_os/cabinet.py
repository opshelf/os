from __future__ import annotations

import logging
from typing import Any

from hardware.gpio import DoorLock
from openshelf_os.exceptions import ConfigError


class Cabinet:
    def __init__(self, *, gpio: Any = None) -> None:
        self._gpio = gpio
        self._locks: dict[str, DoorLock] = {}
        self._logger = logging.getLogger(__name__).getChild("Cabinet")

    def _require_locks(self) -> dict[str, DoorLock]:
        if self._locks:
            return self._locks

        if self._gpio is None:
            raise ConfigError("Cabinet GPIO is not configured")

        lock_specs = {
            "top": ("cabinet_top_unlock", "cabinet_top_locked"),
            "bottom": ("cabinet_bot_unlock", "cabinet_bot_locked"),
        }

        missing = [
            pin_name
            for names in lock_specs.values()
            for pin_name in names
            if self._gpio.has_pin(pin_name) is False
        ]
        if missing:
            raise ConfigError("Cabinet GPIO pins are not configured: " + ", ".join(missing))

        self._locks = {
            name: DoorLock(
                unlock_output=self._gpio.output(unlock_name),
                engaged_input=self._gpio.input(engaged_name),
            )
            for name, (unlock_name, engaged_name) in lock_specs.items()
        }
        return self._locks

    def get_lock_state(self) -> dict[str, bool]:
        locks = self._require_locks()
        top_locked = locks["top"].is_locked()
        bottom_locked = locks["bottom"].is_locked()
        return {
            "top_locked": top_locked,
            "bottom_locked": bottom_locked,
            "locked": top_locked and bottom_locked,
        }

    def get_status(self) -> dict[str, dict[str, bool]]:
        return {"locks": self.get_lock_state()}

    async def lock(self) -> dict[str, bool]:
        locks = self._require_locks()
        for lock in locks.values():
            lock.lock()
        return self.get_lock_state()

    async def unlock(self) -> dict[str, bool]:
        locks = self._require_locks()
        for lock in locks.values():
            lock.unlock()
        return self.get_lock_state()
