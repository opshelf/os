import asyncio
import math
import random
from pathlib import Path

import pytest
import pytest_asyncio
from hardware.controller import SharedModbusController
from hardware.drivers.iclrs_register_types import MotionStatus
from perpetual_os1.common import MotorWithDriver as MotorConfig
from perpetual_os1.motor_axis import MotorAxisIclRs
from pydantic import BaseModel

CONFIG_JSON_FILENAME = "motor_config.json"


class Config(BaseModel):
    motor: MotorConfig


# Config
config = {
    "bus": {
        "motor": {
            "ports": ["/dev/tty.usbserial-B002IJ8G"],
            "baudrate": 115200,
            "parity": "N",
            "timeout": 2.0,
            "retry_delay_s": 0.25,
        }
    }
}

shared_lock = asyncio.Lock()


# --- Fixtures ---
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
def motor_config(config_json_str):
    return Config.model_validate_json(config_json_str).motor


@pytest_asyncio.fixture(scope="module")
async def modbus_controller():
    port = "/dev/tty.usbserial-B002IJ8G"
    baudrate = 115200
    controller = SharedModbusController(port=port, baudrate=baudrate)
    await controller.connect()
    yield controller
    await controller.close()


@pytest_asyncio.fixture(scope="module")
async def motor_axis(modbus_controller, motor_config):
    motor_axis = MotorAxisIclRs(
        controller=modbus_controller, params=motor_config
    )
    yield motor_axis


@pytest_asyncio.fixture(scope="module")
async def random_position_mm(motor_config):
    limit_mm = motor_config.lim
    return random.randint(0, limit_mm)


# --- Tests ---
def test_config_json(motor_config):
    # Test will fail if the fixture 'motor_config' throws any exception --
    # the config json is not valid.
    assert True


def test_init(motor_axis):
    # Test will fail if the fixture 'motor_axis' throws any exception --
    # error while instantiating.
    assert True


@pytest.mark.asyncio(loop_scope="module")
async def test_configure(motor_axis):
    async with shared_lock:
        await motor_axis.configure()


@pytest.mark.asyncio(loop_scope="module")
async def test_clear_alarm(motor_axis):
    async with shared_lock:
        await motor_axis.clear_alarm()
    assert True


@pytest.mark.asyncio(loop_scope="module")
async def test_home(motor_axis):
    async with shared_lock:
        await motor_axis.home()
        pos = await motor_axis.get_position()
    assert math.isclose(0, pos, abs_tol=0.05)


@pytest.mark.parametrize("execution_number", range(5))
@pytest.mark.asyncio(loop_scope="module")
async def test_move(motor_axis, motor_config, execution_number):
    async with shared_lock:
        limit_mm = motor_config.lim
        random_position_mm = random.randint(0, limit_mm)
        target_position_mm = random_position_mm
        await motor_axis.move_to_position(
            position_mm=target_position_mm,
        )
        pos = await motor_axis.get_position()
    assert abs(pos - target_position_mm) < 0.1


@pytest.mark.asyncio(loop_scope="module")
async def test_jog(motor_axis):
    async with shared_lock:
        starting_position_mm = await motor_axis.get_position()
        distance_mm = 10
        await motor_axis.jog(distance_mm=distance_mm)
        final_position_mm = await motor_axis.get_position()
    assert (
        abs(starting_position_mm + distance_mm - final_position_mm)
        < 0.1
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_stop(motor_axis):
    async with shared_lock:
        await motor_axis.home()
        target_position_mm = 30
        move_task = asyncio.create_task(
            motor_axis.move_to_position(
                position_mm=target_position_mm,
            )
        )
        while not move_task.done():
            await asyncio.sleep(0.5)
            await motor_axis.stop()
        pos = await motor_axis.get_position()
    assert abs(pos - target_position_mm) > 1


@pytest.mark.asyncio(loop_scope="module")
async def test_curtain_safety(motor_axis):
    async with shared_lock:
        WAIT_S = 2
        await motor_axis.activate_safety_curtain(True)
        starting_position = await motor_axis.get_position()
        target_position_mm = 50
        move_task = asyncio.create_task(
            motor_axis.move_to_position(
                position_mm=target_position_mm,
            )
        )
        # Motor shouldn't move, since safety curtain is activated.
        # Wait 5 seconds, then deactivate the safety curtain.
        await asyncio.sleep(WAIT_S)
        position_before_safety_curtain_deactivated = (
            await motor_axis.get_position()
        )
        await motor_axis.activate_safety_curtain(False)
        # Motor should now move.
        await move_task
        position_after_move = await motor_axis.get_position()

    assert (abs(position_after_move - target_position_mm) < 0.1) and (
        abs(
            position_before_safety_curtain_deactivated
            - starting_position
        )
        < 0.1
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_curtain_safety_during(motor_axis):
    async with shared_lock:
        WAIT_S = 1
        await motor_axis.home()
        target_position_mm = 50
        move_task = asyncio.create_task(
            motor_axis.move_to_position(
                position_mm=target_position_mm, velocity_mm_per_s=25
            )
        )
        await asyncio.sleep(WAIT_S)
        # Activate curtain safety while move is running, move should pause.
        await motor_axis.activate_safety_curtain(True)
        await asyncio.sleep(WAIT_S)
        status_during_safety_curtain: MotionStatus = (
            await motor_axis.driver.read_status()
        )
        # Deactivate curtain safety while move is running.
        await motor_axis.activate_safety_curtain(False)
        # Motor should now move, completing the interrupted move.
        await move_task
        position_after_move = await motor_axis.get_position()

    assert (abs(position_after_move - target_position_mm) < 0.1) and (
        not status_during_safety_curtain.is_running
    )
