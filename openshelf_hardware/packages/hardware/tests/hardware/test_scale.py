import pytest
from unittest.mock import AsyncMock, call

from hardware.exceptions import ModbusError
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
	"register, zero_ok, weight_ok",
	[
		pytest.param(0x04, True, False, id="zero_ok"),
		pytest.param(0x08, False, True, id="weight_ok"),
		pytest.param(0x0C, True, True, id="both_ok"),
	],
)
async def test_get_calibration_status_decodes_bits(
	mock_scale_controller, register: int, zero_ok: bool, weight_ok: bool
) -> None:
	scale = Scale(mock_scale_controller)
	mock_scale_controller.read_holding_registers.return_value = register

	status = await scale.get_calibration_status()

	assert status["zero_ok"] is zero_ok
	assert status["weight_ok"] is weight_ok
	assert status["raw"] == register


@pytest.mark.asyncio
async def test_get_calibration_status_failure(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.read_holding_registers.return_value = None

	with pytest.raises(RuntimeError, match="Failed to read calibration status"):
		await scale.get_calibration_status()


@pytest.mark.asyncio
async def test_get_calibration_params(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.read_holding_registers.side_effect = [
		0, 6000,
		0, 500,
		1,
		1,
	]

	params = await scale.get_calibration_params()

	assert params["full_scale"] == 6000
	assert params["calibration_weight"] == 500
	assert params["precision"] == 1
	assert params["division"] == 1


@pytest.mark.asyncio
async def test_get_calibration_params_failure(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.read_holding_registers.return_value = None

	with pytest.raises(RuntimeError, match="Failed to read calibration parameters"):
		await scale.get_calibration_params()


@pytest.mark.asyncio
async def test_set_calibration_params(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.write_single_register.return_value = None

	result = await scale.set_calibration_params(
		full_scale=6000,
		calibration_weight=500,
		precision=1,
		division=1,
	)

	assert result is True
	assert mock_scale_controller.write_single_register.call_args_list == [
		call(1, Scale.REG_FULL_SCALE_HI, 0),
		call(1, Scale.REG_FULL_SCALE_LO, 6000),
		call(1, Scale.REG_CAL_WEIGHT_HI, 0),
		call(1, Scale.REG_CAL_WEIGHT_LO, 500),
		call(1, Scale.REG_PRECISION, 1),
		call(1, Scale.REG_DIVISION, 1),
	]


@pytest.mark.asyncio
async def test_set_calibration_params_failure(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.write_single_register.side_effect = Exception("Write failed")

	result = await scale.set_calibration_params(
		full_scale=6000,
		calibration_weight=500,
	)

	assert result is False


@pytest.mark.asyncio
async def test_calibrate_zero_success(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.write_single_register.return_value = None
	mock_scale_controller.read_holding_registers.return_value = 0x04

	result = await scale.calibrate_zero(timeout=1.0)

	assert result["success"] is True
	assert result["status"]["zero_ok"] is True


@pytest.mark.asyncio
async def test_calibrate_zero_write_failure(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.write_single_register.side_effect = Exception("Write failed")

	result = await scale.calibrate_zero(timeout=1.0)

	assert result["success"] is False
	assert "Failed to write" in result["error"]


@pytest.mark.asyncio
async def test_calibrate_zero_timeout(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.write_single_register.return_value = None
	mock_scale_controller.read_holding_registers.return_value = 0x00

	result = await scale.calibrate_zero(timeout=0.3)

	assert result["success"] is False
	assert "timeout" in result["error"].lower()


@pytest.mark.asyncio
async def test_calibrate_weight_success(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.write_single_register.return_value = None
	mock_scale_controller.read_holding_registers.return_value = 0x08

	result = await scale.calibrate_weight(timeout=1.0)

	assert result["success"] is True
	assert result["status"]["weight_ok"] is True


@pytest.mark.asyncio
async def test_calibrate_weight_write_failure(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.write_single_register.side_effect = Exception("Write failed")

	result = await scale.calibrate_weight(timeout=1.0)

	assert result["success"] is False
	assert "Failed to write" in result["error"]


@pytest.mark.asyncio
async def test_calibrate_weight_timeout(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.write_single_register.return_value = None
	mock_scale_controller.read_holding_registers.return_value = 0x00

	result = await scale.calibrate_weight(timeout=0.3)

	assert result["success"] is False
	assert "timeout" in result["error"].lower()


@pytest.mark.asyncio
async def test_read_weight_fast_uses_one_transaction_and_no_retry(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.read_holding_register_block.return_value = (0, 2457, 1, 0x11)

	reading = await scale.read_weight_fast()

	assert reading.grams == pytest.approx(245.7)
	assert reading.decimals == 1
	assert reading.stable is True
	assert reading.valid is True
	mock_scale_controller.read_holding_register_block.assert_awaited_once_with(1, 0, 4, attempts=1)


@pytest.mark.asyncio
async def test_read_weight_fast_reports_a_failed_transaction(mock_scale_controller):
	scale = Scale(mock_scale_controller)
	mock_scale_controller.read_holding_register_block.side_effect = ModbusError("no response")

	with pytest.raises(ModbusError):
		await scale.read_weight_fast()
