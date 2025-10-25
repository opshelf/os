from __future__ import annotations
from typing import Optional

try:
    import RPi.GPIO as GPIO  # type: ignore[import]
except ModuleNotFoundError as import_error:
    GPIO = None  # type: ignore[assignment]
    _GPIO_IMPORT_ERROR = import_error
else:
    _GPIO_IMPORT_ERROR = None
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)


def _ensure_gpio() -> None:
    if GPIO is None:
        raise RuntimeError(
            "RPi.GPIO is not available. This module must run on a Raspberry Pi "
            "with the RPi.GPIO package installed."
        ) from _GPIO_IMPORT_ERROR


def configure_curtain_input(pin: int, *, pull: Optional[str] = "down") -> None:
    """
    Configure the GPIO pin that reports the curtain sensor state.

    Args:
        pin: BCM pin number connected to the curtain sensor.
        pull: One of 'up', 'down', or 'off'. Defaults to 'down'.
    """
    _ensure_gpio()

    pull_options = {
        None: GPIO.PUD_OFF,
        "off": GPIO.PUD_OFF,
        "down": GPIO.PUD_DOWN,
        "up": GPIO.PUD_UP,
    }

    if pull not in pull_options:
        raise ValueError(f"Invalid pull option '{pull}'. Use 'up', 'down', or 'off'.")

    GPIO.setup(pin, GPIO.IN, pull_up_down=pull_options[pull])


def read_curtain_state(pin: int) -> bool:
    """
    Read the current curtain sensor state.

    Args:
        pin: BCM pin number previously configured with configure_curtain_input.

    Returns:
        True if the pin reads HIGH, False otherwise.
    """
    _ensure_gpio()
    return GPIO.input(pin) == GPIO.HIGH


def cleanup_curtain_pin(pin: Optional[int] = None) -> None:
    """
    Release GPIO resources associated with the curtain sensor.

    Args:
        pin: Optional BCM pin to clean up. If omitted, all pins are reset.
    """
    _ensure_gpio()
    if pin is None:
        GPIO.cleanup()
    else:
        GPIO.cleanup(pin)
