import pytest
from unittest.mock import AsyncMock, Mock

from hardware.laser import LaserSensor


@pytest.fixture
def mock_laser_controller() -> AsyncMock:
    controller = AsyncMock()
    controller.read_holding_register_block = AsyncMock(return_value=(0, 56700))
    controller.read_input_register_block = AsyncMock(return_value=(0, 56700))
    controller.connect = AsyncMock()
    controller.close = AsyncMock()
    controller.client = Mock()
    controller.client.connected = True
    return controller


@pytest.mark.asyncio
async def test_laser_read_distance_mm(mock_laser_controller):
    laser = LaserSensor(
        modbus_controller=mock_laser_controller,
        device_id=1,
        read_function_code=3,
        register_address=0,
        register_count=2,
        distance_register_index=1,
        distance_scale_mm=0.001,
    )

    distance_mm = await laser.read_distance_mm()

    assert distance_mm == pytest.approx(56.7)
    mock_laser_controller.read_holding_register_block.assert_called_once_with(
        unit_id=1,
        address=0,
        count=2,
    )


