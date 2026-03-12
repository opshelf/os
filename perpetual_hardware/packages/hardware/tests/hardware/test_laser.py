import pytest
from unittest.mock import AsyncMock, Mock

from hardware.laser import LaserSensor


@pytest.fixture
def mock_laser_controller() -> AsyncMock:
    controller = AsyncMock()
    controller.read_holding_register_block = AsyncMock(return_value=(0, 5670))
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
        register_address=0,
        register_count=2,
        distance_register_index=1,
        distance_scale_mm=0.01,
    )

    distance_mm = await laser.read_distance_mm()

    assert distance_mm == pytest.approx(56.7)
    mock_laser_controller.read_holding_register_block.assert_called_once_with(
        unit_id=1,
        address=0,
        count=2,
    )


@pytest.mark.asyncio
async def test_laser_initialize_returns_probe_distance(mock_laser_controller):
    laser = LaserSensor(
        modbus_controller=mock_laser_controller,
        device_id=1,
        register_address=0,
        register_count=2,
        distance_register_index=1,
        distance_scale_mm=0.01,
    )

    distance_mm = await laser.initialize()

    assert distance_mm == pytest.approx(56.7)


@pytest.mark.asyncio
async def test_laser_read_distance_mm_or_none_returns_none_on_failure(mock_laser_controller):
    mock_laser_controller.read_holding_register_block.side_effect = RuntimeError("read failed")
    laser = LaserSensor(
        modbus_controller=mock_laser_controller,
        device_id=1,
        register_address=0,
        register_count=2,
        distance_register_index=1,
        distance_scale_mm=0.01,
    )

    distance_mm = await laser.read_distance_mm_or_none()

    assert distance_mm is None


def test_laser_validates_distance_register_index(mock_laser_controller):
    with pytest.raises(ValueError, match="distance_register_index must be within register_count"):
        LaserSensor(
            modbus_controller=mock_laser_controller,
            device_id=1,
            register_address=0,
            register_count=2,
            distance_register_index=2,
            distance_scale_mm=0.01,
        )
