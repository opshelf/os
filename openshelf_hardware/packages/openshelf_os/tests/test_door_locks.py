from hardware.gpio import GpioManager
from hardware.gpio.types import GpioPinSpec
from openshelf_os.cabinet import Cabinet

import pytest


def _door_lock_manager() -> GpioManager:
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
			"cabinet_top_locked": GpioPinSpec(
				name="cabinet_top_locked",
				board="rpi",
				pin=17,
				direction="input",
				inverted=True,
			),
			"cabinet_bot_locked": GpioPinSpec(
				name="cabinet_bot_locked",
				board="rpi",
				pin=27,
				direction="input",
				inverted=True,
			),
		},
		fake=True,
	)


@pytest.mark.asyncio
async def test_cabinet_locks_and_unlocks_both_door_locks() -> None:
	gpio = _door_lock_manager()
	cabinet = Cabinet(gpio=gpio)
	gpio.set_fake_input("cabinet_top_locked", True)
	gpio.set_fake_input("cabinet_bot_locked", True)

	locked = await cabinet.lock()

	assert locked == {"top_locked": True, "bottom_locked": True, "locked": True}
	assert cabinet.get_status() == {"locks": locked}
	assert gpio.output("cabinet_top_unlock").read() is False
	assert gpio.output("cabinet_bot_unlock").read() is False

	gpio.set_fake_input("cabinet_top_locked", False)
	gpio.set_fake_input("cabinet_bot_locked", False)
	unlocked = await cabinet.unlock()

	assert unlocked == {"top_locked": False, "bottom_locked": False, "locked": False}
	assert gpio.output("cabinet_top_unlock").read() is True
	assert gpio.output("cabinet_bot_unlock").read() is True
