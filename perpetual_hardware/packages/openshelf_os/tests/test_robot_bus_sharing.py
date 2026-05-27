from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

import openshelf_os.robot as robot_module


def make_bus_settings() -> dict[str, dict[str, object]]:
	return {
		"motor": {
			"ports": ["/dev/ttyUSB1"],
			"baudrate": 115200,
			"parity": "N",
			"timeout": 2.0,
			"retry_delay_s": 0.25,
		},
		"scale": {
			"ports": ["/dev/ttyUSB0"],
			"baudrate": 9600,
			"parity": "N",
			"timeout": 2.0,
			"retry_delay_s": 0.25,
		},
		"laser": {
			"ports": ["/dev/ttyUSB1"],
			"baudrate": 115200,
			"parity": "N",
			"timeout": 2.0,
			"retry_delay_s": 0.25,
		},
	}


def test_create_controller_for_bus_reuses_motor_controller_for_matching_laser(monkeypatch):
	created: list[tuple[str, int, str, float]] = []

	class FakeController:
		def __init__(self, port: str, baudrate: int, parity: str = "N", timeout: float = 2.0) -> None:
			created.append((port, baudrate, parity, timeout))

	monkeypatch.setattr(robot_module, "Controller", FakeController)

	robot = robot_module.Robot.__new__(robot_module.Robot)
	robot._bus_settings = make_bus_settings()
	robot._motorbus = FakeController("/dev/ttyUSB1", 115200)
	robot._scalebus = FakeController("/dev/ttyUSB0", 9600)

	laserbus = robot_module.Robot._create_controller_for_bus(robot, "laser")

	assert laserbus is robot._motorbus
	assert created == [
		("/dev/ttyUSB1", 115200, "N", 2.0),
		("/dev/ttyUSB0", 9600, "N", 2.0),
	]


@pytest.mark.asyncio
async def test_configure_skips_duplicate_laser_connect_when_sharing_motor_bus():
	robot = robot_module.Robot.__new__(robot_module.Robot)
	shared_motor_bus = object()
	scale_bus = object()
	robot._motorbus = shared_motor_bus
	robot._scalebus = scale_bus
	robot._laserbus = shared_motor_bus
	robot._bus_settings = make_bus_settings()
	robot._logger = logging.getLogger("test_robot_bus_sharing")
	robot._connect_controller = AsyncMock()
	robot._tool = SimpleNamespace(configure=AsyncMock())
	robot._io = SimpleNamespace(configure=AsyncMock())
	robot._get_motor_by_name = lambda _name: None
	robot._config = SimpleNamespace(
		robot=SimpleNamespace(bus=SimpleNamespace(laser=object())),
		get=lambda key, default=None: {
			"robot.io.scale": SimpleNamespace(enabled=True),
			"robot.tool.laser": SimpleNamespace(enabled=True),
		}.get(key, default),
	)

	await robot_module.Robot.configure(robot)

	assert robot._connect_controller.await_args_list == [
		call("motor", shared_motor_bus, robot._bus_settings["motor"]),
		call("scale", scale_bus, robot._bus_settings["scale"]),
	]
	robot._tool.configure.assert_awaited_once()
	robot._io.configure.assert_awaited_once()
