import asyncio
import time
from pathlib import Path

import pytest
import pytest_asyncio
from hardware.controller import SharedModbusController
from hardware.drivers.hepg import Hepg, HepgConfig
from hardware.drivers.hepg_register_types import ControlFunction
from pydantic import BaseModel, ValidationError

CONFIG_JSON_FILENAME = "hepg_config.json"


class Config(BaseModel):
    gripper: HepgConfig


shared_lock = asyncio.Lock()


@pytest.fixture(scope="module")
def config_json_path(request):
    tests_path = Path(request.module.__file__).parent
    json_file_path = tests_path / "data" / CONFIG_JSON_FILENAME
    return json_file_path


@pytest.fixture(scope="module")
def config_json_str(config_json_path):
    try:
        with open(config_json_path, "r", encoding="utf-8") as f:
            json_string = f.read()
    except FileNotFoundError:
        print(f"Error: The file '{config_json_path}' was not found.")
    except Exception as e:
        print(f"An error occured: {e}")
    else:
        return json_string


@pytest.fixture(scope="module")
def gripper_config(config_json_str):
    try:
        return Config.model_validate_json(config_json_str).gripper
    except ValidationError:
        return None


@pytest_asyncio.fixture(scope="module")
async def modbus_controller():
    port = "/dev/tty.usbserial-B002IJ8G"
    baudrate = 115200
    controller = SharedModbusController(port=port, baudrate=baudrate)
    await controller.connect()
    yield controller
    await controller.close()


@pytest_asyncio.fixture(scope="module")
async def driver(modbus_controller, gripper_config):
    driver = Hepg(controller=modbus_controller, params=gripper_config)
    yield driver


# --- Tests ---
def test_read_config_json(config_json_str):
    try:
        Config.model_validate_json(config_json_str)
    except ValidationError as e:
        print(f"Error: config file not valid: {e}")
        assert False
    else:
        assert True


@pytest.mark.asyncio(loop_scope="module")
async def test_read_register(driver):
    async with shared_lock:
        register = await driver.client.read_register("slave id")
        assert register.modbus[1] == 9


@pytest.mark.asyncio(loop_scope="module")
async def test_configure(driver):
    async with shared_lock:
        await driver.configure()


@pytest.mark.asyncio(loop_scope="module")
async def test_move(driver):
    async with shared_lock:
        await driver.move(ControlFunction.PRESET_1)
