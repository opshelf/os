from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openshelf_os.robot.core import Robot
from openshelf_os.robot.status import RobotCommandError


@pytest.mark.asyncio
@pytest.mark.parametrize("weight", [0.0, 5.0, -5.0])
async def test_store_rejects_motion_when_scale_reports_empty_io_box(weight: float) -> None:
    robot = Robot.__new__(Robot)
    robot._logger = MagicMock()
    robot._state = SimpleNamespace(current_cell=None)
    robot._operation_context = {}
    robot._demo_cell_map = None
    robot._io = SimpleNamespace(get_weight=AsyncMock(return_value=weight))
    robot._grid = MagicMock()
    robot.io_pick = AsyncMock()

    result = await robot.store(
        {
            "upca": "123",
            "name": "test item",
            "dimensions": {"width": 10, "height": 20, "length": 30},
        }
    )

    assert isinstance(result, RobotCommandError)
    assert result.error == "Place an item in the IO box before storing"
    robot._grid.find_best_slot_for_item.assert_not_called()
    robot.io_pick.assert_not_awaited()
