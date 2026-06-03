from __future__ import annotations

from dataclasses import dataclass

from hardware.gpio.types import DigitalInput, DigitalOutput, GpioSubscription


@dataclass
class DoorRelease:
    unlock_output: DigitalOutput
    engaged_input: DigitalInput

    def set_passive(self) -> None:
        self.unlock_output.write(False)

    def release(self) -> None:
        self.unlock_output.write(True)

    def is_engaged(self) -> bool:
        return self.engaged_input.read()

    def subscribe_engaged(self, callback, *, emit_initial: bool = False) -> GpioSubscription:
        return self.engaged_input.subscribe(callback, emit_initial=emit_initial)
