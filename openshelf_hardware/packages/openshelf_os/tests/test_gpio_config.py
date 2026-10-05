

from openshelf_os.config.models import (
    GpioConfig,
)
from hardware.gpio.manager import GpioManager


def power_pin(pin: int) -> dict[str, object]:
    return {"board": "pca9535", "expander": "pca9535_1", "pin": pin, "direction": "output"}


def test_legacy_power_control_pin_is_renamed_to_pwr_out0() -> None:
    """A config declaring pins replaces the defaults, so the old key must migrate."""
    gpio = GpioConfig.model_validate(
        {"pins": {"power_control": power_pin(12), "pwr_out1": power_pin(13)}}
    )

    assert "power_control" not in gpio.pins
    assert gpio.pins["pwr_out0"].pin == 12
    assert gpio.pins["pwr_out1"].pin == 13


def test_explicit_pwr_out0_wins_over_the_legacy_alias() -> None:
    raw_pins = {"power_control": power_pin(12), "pwr_out0": power_pin(12)}
    gpio = GpioConfig.model_validate({"pins": raw_pins})

    assert "power_control" not in gpio.pins
    assert gpio.pins["pwr_out0"].pin == 12
    assert GpioManager.from_config(gpio, fake=True).has_pin("pwr_out0")


