import asyncio

import pytest
from hardware.controller import SharedModbusController
from hardware.drivers.iclrs import Iclrs, IclrsConfig
from hardware.drivers.iclrs import registers as ALL_REGISTERS
from hardware.drivers.iclrs_register_types import (
    AlarmCode,
    DigitalOutputFunction,
    MotorDirection,
    NormallyOpenedOrClosed,
)
from hardware.exceptions import (
    MotorConfigError,
    MotorError,
    MotorTimeoutError,
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
                    {"name": "pulse per revolution", "value": 11111},
                    {
                        "name": "so2",
                        "value": {
                            "function": DigitalOutputFunction.PATH_COMPLETE,
                            "mode": NormallyOpenedOrClosed.NORMALLY_CLOSED,
                        },
                    },
                    {"name": "delay of brake released", "value": 100},
                    {"name": "delay of brake locked", "value": 100},
                    {"name": "peak current", "value": 16},
                    {
                        "name": "motor direction",
                        "value": MotorDirection.CCW,
                    },
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
async def test_setup():
    async with shared_lock:
        await controller.connect()
        await driver.reset_registers()
    assert True


@pytest.mark.asyncio(loop_scope="session")
async def test_read_register():
    async with shared_lock:
        register = await driver.read_register("pulse per revolution")
        assert register.modbus[1] == 10000


@pytest.mark.asyncio(loop_scope="session")
async def test_write_register():
    async with shared_lock:
        value = 12345
        original_register = await driver.read_register(
            "pulse per revolution"
        )
        # Write and read-back
        await driver.write_register(
            "pulse per revolution", value=value
        )
        register = await driver.read_register("pulse per revolution")
        # Restore
        await driver.write_register(
            "pulse per revolution", value=original_register.modbus[1]
        )
        assert register.modbus[1] == value


@pytest.mark.asyncio(loop_scope="session")
async def test_configure():
    async with shared_lock:
        await driver.configure()
        register = await driver.read_register("peak current")
        assert register.value == 16


@pytest.mark.asyncio(loop_scope="session")
async def test_home():
    async with shared_lock:
        await driver.home()
        position = await driver.read_position()
        error = abs(0 - position)
    assert error <= 20


# @pytest.mark.asyncio(loop_scope="session")
# async def test_clear_alarm():
#     async with shared_lock:
#         # Force an alarm
#         try:
#             await driver.move_to_position(0xFFFFF, 300, 200)
#         except MotorError:
#             # Stall should have been detected.
#             # Alarm code should be 32 ("stall torque").
#             pass
#         alarm = await driver.read_alarm()
#         await driver.clear_alarms()
#         alarm = await driver.read_alarm()
#         # Reset position
#         await driver.home()
#     assert alarm.typed.code == AlarmCode.NO_ALARM


@pytest.mark.asyncio(loop_scope="session")
async def test_move():
    async with shared_lock:
        await driver.clear_alarms()
        await driver.move_to_position_no_wait(0xFFFF, 300, 2000)
        wait_task = asyncio.create_task(driver.wait_for_idle())

        while not wait_task.done():
            await asyncio.sleep(0.5)

        position = await driver.read_position()
        error = abs(0xFFFF - position)
    assert error <= 20


@pytest.mark.asyncio(loop_scope="session")
async def test_stop():
    async with shared_lock:
        await driver.clear_alarms()
        await driver.move_to_position_no_wait(0x1FFFF, 300, 2000)
        wait_task = asyncio.create_task(driver.wait_for_idle())

        while not wait_task.done():
            await asyncio.sleep(1)
            await driver.stop()

        status = await driver.read_status()
    assert not status.is_running


# @pytest.mark.asyncio(loop_scope="session")
# async def test_stop():
#     async with shared_lock:
#         await driver.move_to_position_no_wait(0xFFFFF, 300, 2000)
#         await asyncio.sleep(2)
#         await driver.stop()
#         await driver.wait_for_idle()
#         status = await driver.read_status()
#         print(status)
#         # await driver.move_to_position(1000, 300, 2000)
#     assert not status.is_running


# @pytest.mark.asyncio(loop_scope="session")
# async def test_read_status():
#     async with shared_lock:
#         status = await driver.read_status()
#     assert status.is_enabled


# @pytest.mark.asyncio(loop_scope="session")
# async def test_stop():
#     async with shared_lock:
#         await driver.move_to_position(0xFFFFF, 300, 2000)
#         await asyncio.sleep(2)
#         await driver.stop()
#         await driver.wait_for_idle()
#         status = await driver.read_status()
#         await driver.move_to_position(0xFFF, 300, 2000)
#     assert not status.is_running


# @pytest.mark.asyncio(loop_scope="session")
# async def test_stop():
#     async with shared_lock:
#         await driver.move_to_position_no_wait(0xFFFFF, 300, 2000)
#         await asyncio.sleep(2)
#         await driver.stop()
#         await driver.wait_for_idle()
#         status = await driver.read_status()
#         await driver.move_to_position(0xFFF, 300, 2000)
#     assert not status.is_running


# @pytest.mark.asyncio(loop_scope="session")
# async def test_limit_exceeds():
#     async with shared_lock:
#         limit = 0x1FFFF
#         driver.set_position_limit_mm(limit=limit)
#         position = driver._position_validator(value=0xFFFFFFFF)
#     assert position.clamped_value == limit


# @pytest.mark.asyncio(loop_scope="session")
# async def test_limit_within():
#     async with shared_lock:
#         limit = 0x1FFFF
#         driver.set_position_limit_mm(limit=limit)
#         position = driver._position_validator(value=1)
#     assert position.clamped_value == position.value


# @pytest.mark.asyncio(loop_scope="session")
# async def test_ppr():
#     async with shared_lock:
#         ppr = Iclrs.calculate_mpr_to_ppr(validated_motor_config.mpr)
#         print(ppr)
#         await driver.home()
#         await driver.write_register("pulse per revolution", value=ppr)
#         await driver.move_to_position(20000, 300, 2000)
#     assert True


# @pytest.mark.asyncio(loop_scope="session")
# async def test_get_defaults():
#     # Reset parameters
#     register = await driver.write_control_word_and_read_status(
#         ControlWordFunction.RESET_ALL_PARAMETERS
#     )
#     if register.value == ControlWordStatus.OK:
#         read_registers = []
#         # Scan defaults
#         for register in ALL_REGISTERS:
#             name = register.name
#             read_register = await driver.read_register(name)
#             read_registers.append(read_register)
#         print("\nname,address,default")
#         for register in read_registers:
#             print(
#                 f"{register.name},{register.address},{register.value}"
#             )
#     assert True


# @pytest.mark.asyncio(loop_scope="session")
# async def test_crash():
#     async with shared_lock:
#         # Purposely crash to test crash response.
#         crashed = False
#         try:
#             await driver.move_to_position(0xFFFFF, 300, 2000)
#         except MotorError:
#             crashed = True
#     assert crashed == True


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
