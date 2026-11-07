from types import SimpleNamespace
from unittest.mock import AsyncMock

from openshelf_os.iobox import IOBox
from openshelf_os.robot.core import Robot


async def test_stop_motion_halts_the_motors_but_keeps_the_safety_monitors_running() -> None:
    """A laser calibration abort halts motion with stop_motion; if it shut the curtain or cabinet
    monitors down the way stop() does, the robot would keep running unguarded until a restart."""
    curtain_monitor = SimpleNamespace(stop=AsyncMock())
    io = IOBox.__new__(IOBox)
    io._md = SimpleNamespace(stop=AsyncMock())
    io._curtain_monitors = [curtain_monitor]

    robot = Robot.__new__(Robot)
    robot._tool = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("drive fault")))
    robot._io = io
    robot._cabinet = SimpleNamespace(stop=AsyncMock())

    try:
        await robot.stop_motion()
    except RuntimeError:
        pass

    robot._tool.stop.assert_awaited_once()
    io._md.stop.assert_awaited_once()
    curtain_monitor.stop.assert_not_awaited()
    assert io._curtain_monitors == [curtain_monitor]
    robot._cabinet.stop.assert_not_awaited()
