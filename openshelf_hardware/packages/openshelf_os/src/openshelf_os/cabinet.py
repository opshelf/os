from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from hardware.gpio.types import DigitalInput, DigitalOutput
from openshelf_os.exceptions import ConfigError


@dataclass
class CabinetDoorLock:
    unlock_output: DigitalOutput
    closed_input: DigitalInput

    def lock(self) -> None:
        self.unlock_output.write(False)

    def unlock(self) -> None:
        self.unlock_output.write(True)

    def is_closed(self) -> bool:
        return self.closed_input.read()


class Cabinet:
    def __init__(self, *, gpio: Any = None) -> None:
        self._gpio = gpio
        self._door_locks: dict[str, CabinetDoorLock] = {}
        self._logger = logging.getLogger(__name__).getChild("Cabinet")

    def _require_door_locks(self) -> dict[str, CabinetDoorLock]:
        if self._door_locks:
            return self._door_locks

        if self._gpio is None:
            raise ConfigError("Cabinet GPIO is not configured")

        lock_specs = {
            "top": ("cabinet_top_unlock", "cabinet_top_closed", "cabinet_top_locked"),
            "bottom": ("cabinet_bot_unlock", "cabinet_bot_closed", "cabinet_bot_locked"),
        }

        missing = []
        resolved_specs = {}
        for name, (unlock_name, closed_name, legacy_closed_name) in lock_specs.items():
            if self._gpio.has_pin(unlock_name) is False:
                missing.append(unlock_name)

            resolved_closed_name = self._resolve_pin_name(closed_name, legacy_closed_name)
            if resolved_closed_name is None:
                missing.append(closed_name)
            else:
                resolved_specs[name] = (unlock_name, resolved_closed_name)

        if missing:
            raise ConfigError("Cabinet GPIO pins are not configured: " + ", ".join(missing))

        self._door_locks = {
            name: CabinetDoorLock(
                unlock_output=self._gpio.output(unlock_name),
                closed_input=self._gpio.input(closed_name),
            )
            for name, (unlock_name, closed_name) in resolved_specs.items()
        }
        return self._door_locks

    def _resolve_pin_name(self, preferred_name: str, legacy_name: str) -> str | None:
        if self._gpio.has_pin(preferred_name) is True:
            return preferred_name

        if self._gpio.has_pin(legacy_name) is True:
            return legacy_name

        return None

    def get_door_state(self) -> dict[str, bool]:
        door_locks = self._require_door_locks()
        top_closed = door_locks["top"].is_closed()
        bottom_closed = door_locks["bottom"].is_closed()
        return {
            "top_closed": top_closed,
            "bottom_closed": bottom_closed,
            "closed": top_closed and bottom_closed,
        }

    def get_status(self) -> dict[str, dict[str, bool]]:
        return {"doors": self.get_door_state()}

    async def lock(self) -> dict[str, bool]:
        door_locks = self._require_door_locks()
        for door_lock in door_locks.values():
            door_lock.lock()
        return self.get_door_state()

    async def unlock(self) -> dict[str, bool]:
        door_locks = self._require_door_locks()
        for door_lock in door_locks.values():
            door_lock.unlock()
        return self.get_door_state()
