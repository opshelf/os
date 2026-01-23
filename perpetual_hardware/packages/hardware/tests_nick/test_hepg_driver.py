from pathlib import Path

import pytest
import pytest_asyncio
from hardware.controller import SharedModbusController
from hardware.drivers.hepg import Hepg, HepgConfig
from pydantic import BaseModel, ValidationError

CONFIG_JSON_FILENAME = "hepg_config.json"


class Config(BaseModel):
    gripper: HepgConfig


@pytest.fixture
def config_json_path(request):
    tests_path = request.path.parent
    json_file_path = tests_path / "data" / CONFIG_JSON_FILENAME
    return json_file_path


@pytest.fixture
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


@pytest.fixture
def gripper_config(config_json_str):
    try:
        return Config.model_validate_json(config_json_str).gripper
    except ValidationError:
        return None


@pytest_asyncio.fixture
async def modbus_controller():
    port = "/dev/tty.usbserial-B002IJ8G"
    baudrate = 115200

    controller = SharedModbusController(port=port, baudrate=baudrate)
    controller.connect()
    return controller


@pytest_asyncio.fixture
def hepg_driver(modbus_controller, gripper_config):
    driver = Hepg(controller=modbus_controller, params=gripper_config)
    return driver


def test_read_config_json(config_json_str):
    try:
        Config.model_validate_json(config_json_str)
    except ValidationError as e:
        print(f"Error: config file not valid: {e}")
        assert False
    else:
        assert True


@pytest.mark.asyncio(loop_scope="session")
async def test_read_register(hepg_driver):
    register = await hepg_driver.read_register("control")
    print(register)
    assert register.modbus[1] == 10000
