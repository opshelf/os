from hardware.gpio import GpioManager
from hardware.gpio.types import GpioPinSpec
from openshelf_os.cabinet import Cabinet

import pytest


def _door_lock_manager(*, legacy_sensor_names: bool = False) -> GpioManager:
	top_closed_name = "cabinet_top_locked" if legacy_sensor_names is True else "cabinet_top_closed"
	bottom_closed_name = "cabinet_bot_locked" if legacy_sensor_names is True else "cabinet_bot_closed"
	return GpioManager(
		{
			"cabinet_top_unlock": GpioPinSpec(
				name="cabinet_top_unlock",
				board="rpi",
				pin=5,
				direction="output",
				initial=False,
				shutdown=False,
			),
			"cabinet_bot_unlock": GpioPinSpec(
				name="cabinet_bot_unlock",
				board="rpi",
				pin=6,
				direction="output",
				initial=False,
				shutdown=False,
			),
			top_closed_name: GpioPinSpec(
				name=top_closed_name,
				board="rpi",
				pin=17,
				direction="input",
				inverted=True,
			),
			bottom_closed_name: GpioPinSpec(
				name=bottom_closed_name,
				board="rpi",
				pin=27,
				direction="input",
				inverted=True,
			),
		},
		fake=True,
	)


@pytest.mark.asyncio
async def test_cabinet_lock_commands_report_door_closed_sensors() -> None:
	gpio = _door_lock_manager()
	cabinet = Cabinet(gpio=gpio)
	gpio.set_fake_input("cabinet_top_closed", True)
	gpio.set_fake_input("cabinet_bot_closed", True)

	closed = await cabinet.lock()

	assert closed == {"top_closed": True, "bottom_closed": True, "closed": True}
	assert cabinet.get_status() == {"doors": closed}
	assert gpio.output("cabinet_top_unlock").read() is False
	assert gpio.output("cabinet_bot_unlock").read() is False

	gpio.set_fake_input("cabinet_top_closed", False)
	gpio.set_fake_input("cabinet_bot_closed", False)
	open_state = await cabinet.unlock()

	assert open_state == {"top_closed": False, "bottom_closed": False, "closed": False}
	assert gpio.output("cabinet_top_unlock").read() is True
	assert gpio.output("cabinet_bot_unlock").read() is True


@pytest.mark.asyncio
async def test_cabinet_supports_legacy_locked_sensor_pin_names() -> None:
	gpio = _door_lock_manager(legacy_sensor_names=True)
	cabinet = Cabinet(gpio=gpio)
	gpio.set_fake_input("cabinet_top_locked", False)
	gpio.set_fake_input("cabinet_bot_locked", True)

	assert await cabinet.unlock() == {"top_closed": False, "bottom_closed": True, "closed": False}
