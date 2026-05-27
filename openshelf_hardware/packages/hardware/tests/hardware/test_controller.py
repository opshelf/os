import pytest
from unittest.mock import AsyncMock, Mock
from hardware.controller import SharedModbusController
from hardware.exceptions import ModbusConnectionError, ModbusError

@pytest.mark.asyncio
async def test_controller_initialization():
    controller = SharedModbusController("/dev/ttyUSB0", 115200)
    assert controller.port == "/dev/ttyUSB0"
    assert controller.baudrate == 115200
    assert controller.timeout == 2.0
    assert controller.parity == "N"
    assert controller.client is None

@pytest.mark.asyncio
async def test_controller_connect(mock_modbus_client):
    controller = SharedModbusController("/dev/ttyUSB0", 115200)
    controller.client = mock_modbus_client
    await controller.connect()
    mock_modbus_client.connect.assert_called_once()

@pytest.mark.asyncio
async def test_controller_read_holding_registers(mock_modbus_client):
    controller = SharedModbusController("/dev/ttyUSB0", 115200)
    controller.client = mock_modbus_client
    
    # Setup mock response
    result = Mock()
    result.isError.return_value = False
    result.registers = [42]
    mock_modbus_client.read_holding_registers.return_value = result

    # Test successful read
    value = await controller.read_holding_registers(1, 0x100)
    assert value == 42
    mock_modbus_client.read_holding_registers.assert_called_once_with(
        address=0x100, count=1, device_id=1
    )


@pytest.mark.asyncio
async def test_controller_read_holding_register_block(mock_modbus_client):
    controller = SharedModbusController("/dev/ttyUSB0", 115200)
    controller.client = mock_modbus_client

    result = Mock()
    result.isError.return_value = False
    result.registers = [0, 5670]
    mock_modbus_client.read_holding_registers.return_value = result

    values = await controller.read_holding_register_block(1, 0x0000, 2)

    assert values == (0, 5670)
    mock_modbus_client.read_holding_registers.assert_called_once_with(
        address=0x0000, count=2, device_id=1
    )


@pytest.mark.asyncio
async def test_controller_read_input_register_block(mock_modbus_client):
    controller = SharedModbusController("/dev/ttyUSB0", 115200)
    controller.client = mock_modbus_client

    result = Mock()
    result.isError.return_value = False
    result.registers = [65535, 56701]
    mock_modbus_client.read_input_registers.return_value = result

    values = await controller.read_input_register_block(1, 0x0000, 2)

    assert values == (65535, 56701)
    mock_modbus_client.read_input_registers.assert_called_once_with(
        address=0x0000, count=2, device_id=1
    )


@pytest.mark.asyncio
async def test_controller_read_holding_register_block_validates_count(mock_modbus_client):
    controller = SharedModbusController("/dev/ttyUSB0", 115200)
    controller.client = mock_modbus_client

    with pytest.raises(ValueError, match="count must be greater than zero"):
        await controller.read_holding_register_block(1, 0x0000, 0)

@pytest.mark.asyncio
async def test_controller_write_single_register(mock_modbus_client):
    controller = SharedModbusController("/dev/ttyUSB0", 115200)
    controller.client = mock_modbus_client
    
    # Setup mock response
    result = Mock()
    result.isError.return_value = False
    mock_modbus_client.write_register.return_value = result

    # Test successful write
    success = await controller.write_single_register(1, 0x100, 42)
    assert success is None
    mock_modbus_client.write_register.assert_called_once_with(
        address=0x100, value=42, device_id=1
    )

@pytest.mark.asyncio
async def test_controller_error_handling(mock_modbus_client):
    controller = SharedModbusController("/dev/ttyUSB0", 115200)
    controller.client = mock_modbus_client
    
    # Setup mock error response
    result = Mock()
    result.isError.return_value = True
    mock_modbus_client.read_holding_registers.return_value = result

    # Test error handling
    with pytest.raises(ModbusError):
        await controller.read_holding_registers(1, 0x100)


@pytest.mark.asyncio
async def test_controller_retries_modbus_read_error(mock_modbus_client):
    controller = SharedModbusController("/dev/ttyUSB0", 115200)
    controller.client = mock_modbus_client

    error_result = Mock()
    error_result.isError.return_value = True
    ok_result = Mock()
    ok_result.isError.return_value = False
    ok_result.registers = [42]
    mock_modbus_client.read_holding_registers.side_effect = [error_result, ok_result]

    value = await controller.read_holding_registers(1, 0x100)

    assert value == 42
    assert mock_modbus_client.read_holding_registers.call_count == 2


@pytest.mark.asyncio
async def test_controller_reconnects_on_modbus_connection_error(mock_modbus_client):
    controller = SharedModbusController("/dev/ttyUSB0", 115200)
    controller.client = mock_modbus_client

    ok_result = Mock()
    ok_result.isError.return_value = False
    ok_result.registers = [42]
    mock_modbus_client.read_holding_registers.side_effect = [
        ModbusConnectionError("connection lost"),
        ok_result,
    ]
    controller._reconnect = AsyncMock(return_value=True)

    value = await controller.read_holding_registers(1, 0x100)

    assert value == 42
    controller._reconnect.assert_awaited_once()
    assert mock_modbus_client.read_holding_registers.call_count == 2
