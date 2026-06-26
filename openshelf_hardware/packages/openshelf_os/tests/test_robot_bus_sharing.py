from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

import openshelf_os.robot.core as robot_module
from openshelf_os.serial_ports import (
	DEFAULT_LASER_PORT,
	DEFAULT_MOTOR_PORT,
	DEFAULT_SCALE_PORT,
	LEGACY_MOTOR_PORT,
)


def make_bus_settings() -> dict[str, dict[str, object]]:
	return {
		"motor": {
			"port": "/dev/ttyUSB1",
			"baudrate": 115200,
			"parity": "N",
			"timeout": 2.0,
			"retry_delay_s": 0.25,
		},
		"scale": {
			"port": "/dev/ttyUSB0",
			"baudrate": 9600,
			"parity": "N",
			"timeout": 2.0,
			"retry_delay_s": 0.25,
		},
		"laser": {
			"port": "/dev/ttyUSB1",
			"baudrate": 115200,
			"parity": "N",
			"timeout": 2.0,
			"retry_delay_s": 0.25,
		},
	}


def test_prepare_bus_settings_prefers_waveshare_rs485_hub_port():
	robot = robot_module.Robot.__new__(robot_module.Robot)
	robot._config = SimpleNamespace(robot=SimpleNamespace(bus=None))

	settings = robot_module.Robot._prepare_bus_settings(robot)

	assert settings["motor"]["port"] == DEFAULT_MOTOR_PORT
	assert settings["scale"]["port"] == DEFAULT_SCALE_PORT
	assert settings["laser"]["port"] == DEFAULT_LASER_PORT
	assert DEFAULT_MOTOR_PORT in settings["motor"]["fallback_ports"]
	assert LEGACY_MOTOR_PORT in settings["motor"]["fallback_ports"]


def make_bus_config(port: str, baudrate: int = 9600, ports: list[str] | None = None) -> SimpleNamespace:
	return SimpleNamespace(
		port=port,
		ports=ports,
		baudrate=baudrate,
		parity="N",
		timeout=2.0,
		retry_delay_s=0.25,
	)


def test_prepare_bus_settings_preserves_custom_port():
	custom_motor_port = "/dev/custom-motor"
	custom_scale_port = "/dev/custom-scale"
	custom_laser_port = "/dev/custom-laser"
	robot = robot_module.Robot.__new__(robot_module.Robot)
	robot._config = SimpleNamespace(
		robot=SimpleNamespace(
			bus=SimpleNamespace(
				motor=make_bus_config(custom_motor_port, 115200),
				scale=make_bus_config(custom_scale_port),
				laser=make_bus_config(custom_laser_port),
			)
		)
	)

	settings = robot_module.Robot._prepare_bus_settings(robot)

	assert settings["motor"]["port"] == custom_motor_port
	assert settings["scale"]["port"] == custom_scale_port
	assert settings["laser"]["port"] == custom_laser_port
	assert settings["motor"]["fallback_ports"] == [custom_motor_port]


def test_prepare_bus_settings_adds_stable_symlink_fallback_for_legacy_port():
	robot = robot_module.Robot.__new__(robot_module.Robot)
	robot._config = SimpleNamespace(
		robot=SimpleNamespace(
			bus=SimpleNamespace(
				motor=make_bus_config(LEGACY_MOTOR_PORT, 115200),
				scale=make_bus_config("/dev/custom-scale"),
				laser=None,
			)
		)
	)

	settings = robot_module.Robot._prepare_bus_settings(robot)

	assert settings["motor"]["fallback_ports"] == [LEGACY_MOTOR_PORT, DEFAULT_MOTOR_PORT]
	assert settings["scale"]["fallback_ports"] == ["/dev/custom-scale"]


def test_prepare_bus_settings_preserves_legacy_configured_ports():
	robot = robot_module.Robot.__new__(robot_module.Robot)
	robot._config = SimpleNamespace(
		robot=SimpleNamespace(
			bus=SimpleNamespace(
				motor=make_bus_config(
					DEFAULT_MOTOR_PORT,
					115200,
					ports=[DEFAULT_MOTOR_PORT, LEGACY_MOTOR_PORT],
				),
				scale=make_bus_config(
					"/dev/openshelf-rs485-ch2",
					ports=["/dev/openshelf-rs485-ch2", "/dev/ttyUSB0"],
				),
				laser=None,
			)
		)
	)

	settings = robot_module.Robot._prepare_bus_settings(robot)

	assert settings["motor"]["fallback_ports"] == [DEFAULT_MOTOR_PORT, LEGACY_MOTOR_PORT]
	assert settings["scale"]["fallback_ports"] == ["/dev/openshelf-rs485-ch2", "/dev/ttyUSB0"]


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



@pytest.mark.asyncio
async def test_connect_controller_uses_single_custom_port_when_no_fallbacks_configured():
	class FakeController:
		def __init__(self) -> None:
			self.port = ""
			self.baudrate = 0
			self.parity = ""
			self.timeout = 0.0
			self.client = SimpleNamespace(connected=False)
			self.attempts: list[str] = []

		async def connect(self) -> None:
			self.attempts.append(self.port)
			raise OSError("missing")

		async def close(self) -> None:
			pass

	controller = FakeController()
	settings = {
		"port": "/dev/custom-scale",
		"baudrate": 9600,
		"parity": "N",
		"timeout": 2.0,
		"retry_delay_s": 0,
	}

	with pytest.raises(robot_module.ConfigError, match="/dev/custom-scale: missing"):
		await robot_module.Robot._connect_controller(object(), "scale", controller, settings)

	assert controller.attempts == ["/dev/custom-scale"]


@pytest.mark.asyncio
async def test_connect_controller_falls_back_from_legacy_motor_port_to_stable_symlink():
	class FakeController:
		def __init__(self) -> None:
			self.port = ""
			self.baudrate = 0
			self.parity = ""
			self.timeout = 0.0
			self.client = SimpleNamespace(connected=False)
			self.attempts: list[str] = []

		async def connect(self) -> None:
			self.attempts.append(self.port)
			if self.port == LEGACY_MOTOR_PORT:
				raise OSError("missing")
			self.client.connected = True

		async def close(self) -> None:
			pass

	controller = FakeController()
	settings = {
		"port": LEGACY_MOTOR_PORT,
		"fallback_ports": [LEGACY_MOTOR_PORT, DEFAULT_MOTOR_PORT],
		"baudrate": 115200,
		"parity": "N",
		"timeout": 2.0,
		"retry_delay_s": 0,
	}

	await robot_module.Robot._connect_controller(object(), "motor", controller, settings)

	assert controller.attempts == [LEGACY_MOTOR_PORT, DEFAULT_MOTOR_PORT]
