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


@pytest.mark.asyncio
async def test_read_weight_fast_uses_one_transaction_and_no_retry(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.read_holding_register_block.return_value = (0, 2457, 1, 0x11)

	reading = await scale.read_weight_fast()

	assert reading.grams == pytest.approx(245.7)
	assert reading.decimals == 1
	assert reading.stable is True
	assert reading.valid is True
	mock_scale_controller.read_holding_register_block.assert_awaited_once_with(
		1,
		0,
		4,
		attempts=1,
		modbus_retries=0,
		modbus_timeout_s=Scale.FAST_READ_TIMEOUT_S,
	)


