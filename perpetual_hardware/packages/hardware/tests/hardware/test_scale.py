import pytest
from unittest.mock import AsyncMock

from hardware.scale import Scale


@pytest.fixture
def mock_scale_controller():
	controller = AsyncMock()
	controller.read_holding_registers = AsyncMock()
	controller.write_single_register = AsyncMock()
	return controller


@pytest.mark.asyncio
async def test_read_weight_returns_grams_positive(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.read_holding_registers.side_effect = [0, 2457, 1]

	weight = await scale.read_weight()

	assert weight == pytest.approx(245.7)


@pytest.mark.asyncio
async def test_read_weight_returns_absolute_value_for_negative(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.read_holding_registers.side_effect = [0xFFFF, 0xF667, 1]

	weight = await scale.read_weight()

	assert weight == pytest.approx(245.7)


@pytest.mark.asyncio
async def test_read_weight_returns_none_on_failure(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.read_holding_registers.return_value = None

	weight = await scale.read_weight()

	assert weight is None


@pytest.mark.asyncio
async def test_wait_for_stable_weight_returns_on_stable(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.read_holding_registers.side_effect = [
		1,
		0, 2457, 1,
	]

	weight = await scale.wait_for_stable_weight(timeout=1.0, poll_interval=0.01)

	assert weight == pytest.approx(245.7)


@pytest.mark.asyncio
async def test_wait_for_stable_weight_returns_none_on_timeout(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.read_holding_registers.return_value = 0

	weight = await scale.wait_for_stable_weight(timeout=0.1, poll_interval=0.01)

	assert weight is None


@pytest.mark.asyncio
async def test_interpret_status():
	status = Scale._interpret_status(0b00001)
	assert status["stable"] is True
	assert status["zero"] is False
	assert status["valid"] is False

	status = Scale._interpret_status(0b00011)
	assert status["stable"] is True
	assert status["zero"] is True

	status = Scale._interpret_status(0b10001)
	assert status["stable"] is True
	assert status["valid"] is True


@pytest.mark.asyncio
async def test_to_int32_signed():
	assert Scale._to_int32_signed(0, 100) == 100
	assert Scale._to_int32_signed(0xFFFF, 0xFF9C) == -100
	assert Scale._to_int32_signed(0xFFFF, 0xF667) == -2457
