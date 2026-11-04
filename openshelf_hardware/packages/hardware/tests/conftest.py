from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def mock_controller() -> AsyncMock:
    controller = AsyncMock()
    controller.write_single_register = AsyncMock()
    controller.write_registers = AsyncMock()
    controller.read_holding_registers = AsyncMock(return_value=(1 << 5) | (1 << 6))
    controller.read_holding_register_block = AsyncMock(return_value=((1 << 5) | (1 << 6), 0))
    controller.client = Mock()
    controller.client.connected = True
    return controller
