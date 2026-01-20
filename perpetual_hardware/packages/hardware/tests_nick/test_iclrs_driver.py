import asyncio

import pytest
from hardware.controller import SharedModbusController
from hardware.drivers.iclrs import Iclrs, IclrsConfig
from hardware.drivers.iclrs import registers as ALL_REGISTERS
from hardware.drivers.iclrs_register_types import (
    ControlWordFunction,
    ControlWordStatus,
)

# Config
config = {
    "bus": {
        "motor": {
            "ports": ["/dev/tty.usbserial-B002IJ8G"],
            "baudrate": 115200,
            "parity": "N",
            "timeout": 2.0,
            "retry_delay_s": 0.25,
        },
    },
    "tool": {
        "motors": [
            {
                "motor_name": "z_axis",
                "motor_id": 31,
                "enabled": True,
                "lim": 180,
                "mpr": 1.9992,
                "configuration_registers": [
                    {"name": "pulse per revolution", "value": 11111}
                ],
            }
        ]
    },
}

bus_config = config["bus"]["motor"]
controller = SharedModbusController(
    port=bus_config["ports"][0], baudrate=bus_config["baudrate"]
)

motor_config = config["tool"]["motors"][0]
validated_motor_config = IclrsConfig(**motor_config)

driver = Iclrs(controller=controller, params=validated_motor_config)
shared_lock = asyncio.Lock()


# Connect modbus controller
@pytest.mark.asyncio(loop_scope="session")
async def test_connect():
    await controller.connect()
    assert True


# @pytest.mark.asyncio(loop_scope="session")
# async def test_read_register():
#     async with shared_lock:
#         register = await driver.read_register("pulse per revolution")
#         assert register.modbus[1] == 10000


# @pytest.mark.asyncio(loop_scope="session")
# async def test_write_register():
#     async with shared_lock:
#         value = 12345
#         original_register = await driver.read_register(
#             "pulse per revolution"
#         )
#         # Write and read-back
#         await driver.write_register(
#             "pulse per revolution", value=value
#         )
#         register = await driver.read_register("pulse per revolution")
#         # Restore
#         await driver.write_register(
#             "pulse per revolution", value=original_register.modbus[1]
#         )
#         assert register.modbus[1] == value


@pytest.mark.asyncio(loop_scope="session")
async def test_get_defaults():
    # Reset parameters
    register = await driver.write_control_word_and_read_status(
        ControlWordFunction.RESET_ALL_PARAMETERS
    )
    if register.value == ControlWordStatus.OK:
        read_registers = []
        for register in ALL_REGISTERS:
            name = register.name
            read_register = await driver.read_register(name)
            read_registers.append(read_register)
        # Print
        for register in read_registers:
            print(
                f"{register.name},{register.address},{register.value}"
            )
    assert True


# @pytest.mark.asyncio
# async def test_write_register():
#     address = await driver.read_register('pulse per revolution')
#     print(address)
#     assert address == 0x0001


# # Load configuration from JSON file for configuration registers
# config_json_filename = './data/config_hardware_driver.json'
# try:
#   with open(config_json_filename, 'r') as file:
#     data = json.load(file)
# except FileNotFoundError:
#     print(f"Error: The file {config_json_filename} was not found.")
#     raise
# except json.JSONDecodeError as e:
#     print(f"Failed to decode JSON: {e}")
#     raise
# config_registers = data["configuration_registers"]

# @pytest.mark.parametrize("register", config_registers)
# def test_iclrs_basemodel(register):
#   IclrsRegister.model_validate(register)
#   assert True # Passes if there's no exception

# def test_iclrs_register_by_address():
#   register = IclrsRegister(address = 0x01, value = 1234)
#   print(register)
#   assert True # Passes if there's no exception


# def test_iclrs_import_json_to_config():
#     try:
#         with open(config_json_filename, 'r', encoding='utf-8') as f:
#             raw_json = f.read()
#     except FileNotFoundError:
#         print(f"Error: The file {config_json_filename} was not found.")
#     except Exception as e:
#         print(f"An error occurred: {e}")
#     else:
#         validated = IclrsConfig.model_validate_json(raw_json)
#         print(validated)
#         assert True

# @pytest.fixture
# def iclrs_driver():
#     try:
#         with open(config_json_filename, 'r', encoding='utf-8') as f:
#             raw_json = f.read()
#     except FileNotFoundError:
#         print(f"Error: The file {config_json_filename} was not found.")
#     except Exception as e:
#         print(f"An error occurred: {e}")
#     else:
#         iclrs_config = IclrsConfig.model_validate_json(raw_json)
#         return Iclrs(controller = None, params=iclrs_config)

# @pytest.mark.asyncio
# async def test_iclrs_driver_configure(iclrs_driver):
#     await iclrs_driver.configure()
