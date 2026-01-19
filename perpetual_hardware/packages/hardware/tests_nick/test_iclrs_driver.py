import json

import pytest
from hardware.drivers.iclrs import Iclrs, IclrsConfig, IclrsRegister

# Load configuration from JSON file for configuration registers
config_json_filename = './data/config_hardware_driver.json'
try:
  with open(config_json_filename, 'r') as file:
    data = json.load(file)
except FileNotFoundError:
    print(f"Error: The file {config_json_filename} was not found.")
    raise
except json.JSONDecodeError as e:
    print(f"Failed to decode JSON: {e}")
    raise
config_registers = data["configuration_registers"]

@pytest.mark.parametrize("register", config_registers)
def test_iclrs_basemodel(register):
  IclrsRegister.model_validate(register)
  assert True # Passes if there's no exception

def test_iclrs_register_by_address():
  register = IclrsRegister(address = 0x01, value = 1234)
  print(register)
  assert True # Passes if there's no exception


def test_iclrs_import_json_to_config():
    try:
        with open(config_json_filename, 'r', encoding='utf-8') as f:
            raw_json = f.read()
    except FileNotFoundError:
        print(f"Error: The file {config_json_filename} was not found.")
    except Exception as e:
        print(f"An error occurred: {e}")
    else:
        validated = IclrsConfig.model_validate_json(raw_json)
        print(validated)
        assert True

@pytest.fixture
def iclrs_driver():
    try:
        with open(config_json_filename, 'r', encoding='utf-8') as f:
            raw_json = f.read()
    except FileNotFoundError:
        print(f"Error: The file {config_json_filename} was not found.")
    except Exception as e:
        print(f"An error occurred: {e}")
    else:
        iclrs_config = IclrsConfig.model_validate_json(raw_json)
        return Iclrs(controller = None, params=iclrs_config)

@pytest.mark.asyncio
async def test_iclrs_driver_configure(iclrs_driver):
    await iclrs_driver.configure()
