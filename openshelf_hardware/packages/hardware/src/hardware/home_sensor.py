from __future__ import annotations

try:
    import RPi.GPIO as GPIO  # type: ignore[import]

    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
except (ModuleNotFoundError, RuntimeError):
    GPIO = None  # type: ignore[assignment]


class HomeSensor:
    def __init__(self, *, pin: int, inverted: bool = True) -> None:
        if GPIO is None:
            raise RuntimeError('RPi.GPIO not available')

        self.pin = pin
        self._inverted = inverted
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    def read(self) -> bool:
        raw = GPIO.input(self.pin) == GPIO.HIGH  # type: ignore[arg-type]
        return not raw if self._inverted else raw

    def cleanup(self) -> None:
        GPIO.cleanup(self.pin)  # type: ignore[arg-type]
