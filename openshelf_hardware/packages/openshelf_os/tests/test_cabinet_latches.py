import asyncio
from hardware.gpio import GpioManager
from hardware.gpio.types import GpioPinSpec
from openshelf_os.cabinet import Cabinet

import pytest


def _closed_doors() -> dict[str, bool]:
	return {
		"top_closed": True,
		"bottom_closed": True,
		"closed": True,
		"top_open": False,
		"bottom_open": False,
		"open": False,
	}


def _open_doors() -> dict[str, bool]:
	return {
		"top_closed": False,
		"bottom_closed": False,
		"closed": False,
		"top_open": True,
		"bottom_open": True,
		"open": True,
	}


def _engaged_latches() -> dict[str, bool]:
	return {
		"top_released": False,
		"bottom_released": False,
		"released": False,
		"top_engaged": True,
		"bottom_engaged": True,
		"engaged": True,
	}


def _released_latches() -> dict[str, bool]:
	return {
		"top_released": True,
		"bottom_released": True,
		"released": True,
		"top_engaged": False,
		"bottom_engaged": False,
		"engaged": False,
	}


def _door_latch_manager() -> GpioManager:
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
			"cabinet_top_closed": GpioPinSpec(
				name="cabinet_top_closed",
				board="rpi",
				pin=17,
				direction="input",
				inverted=True,
			),
			"cabinet_bot_closed": GpioPinSpec(
				name="cabinet_bot_closed",
				board="rpi",
				pin=27,
				direction="input",
				inverted=True,
			),
		},
		fake=True,
	)


@pytest.mark.asyncio
async def test_cabinet_latch_commands_report_door_closed_sensors() -> None:
	gpio = _door_latch_manager()
	cabinet = Cabinet(gpio=gpio)
	gpio.set_fake_input("cabinet_top_closed", True)
	gpio.set_fake_input("cabinet_bot_closed", True)

	passive_status = await cabinet.set_passive()

	assert passive_status == {"doors": _closed_doors(), "latches": _engaged_latches()}
	assert cabinet.get_status() == passive_status
	assert gpio.output("cabinet_top_unlock").read() is False
	assert gpio.output("cabinet_bot_unlock").read() is False

	gpio.set_fake_input("cabinet_top_closed", False)
	gpio.set_fake_input("cabinet_bot_closed", False)
	released_status = await cabinet.release()

	assert released_status == {"doors": _open_doors(), "latches": _released_latches()}
	assert gpio.output("cabinet_top_unlock").read() is True
	assert gpio.output("cabinet_bot_unlock").read() is True


@pytest.mark.asyncio
async def test_cabinet_door_listener_receives_state_changes() -> None:
	gpio = _door_latch_manager()
	cabinet = Cabinet(gpio=gpio)
	states: list[dict[str, bool]] = []

	gpio.set_fake_input("cabinet_top_closed", True)
	gpio.set_fake_input("cabinet_bot_closed", True)
	await cabinet.configure()
	cabinet.set_door_listener(states.append)

	gpio.set_fake_input("cabinet_top_closed", False)
	await asyncio.sleep(0)

	assert states[-1] == {
		"top_closed": False,
		"bottom_closed": True,
		"closed": False,
		"top_open": True,
		"bottom_open": False,
		"open": True,
	}
	await cabinet.stop()
