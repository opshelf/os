from __future__ import annotations

from dataclasses import dataclass

from hardware.gpio.types import DigitalInput, DigitalOutput, GpioSubscription


@dataclass
class DoorLock:
    unlock_output: DigitalOutput
    engaged_input: DigitalInput

    def lock(self) -> None:
        self.unlock_output.write(False)

    def unlock(self) -> None:
        self.unlock_output.write(True)

    def is_locked(self) -> bool:
        return self.engaged_input.read()

    def subscribe_locked(self, callback, *, emit_initial: bool = False) -> GpioSubscription:
        return self.engaged_input.subscribe(callback, emit_initial=emit_initial)
