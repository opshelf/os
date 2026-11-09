import asyncio
import logging
from types import SimpleNamespace

import pytest

from hardware.exceptions import ModbusConnectionError
from openshelf_os.common import AxisCoordinate
from openshelf_os.faults import FAULT_MESSAGES
from openshelf_os.robot import Robot
from openshelf_os.robot.commands import MoveCommand
from openshelf_os.robot.status import RobotCommandError, RobotCommandSuccessful
from openshelf_os.storage import CellItem, Dimensions, StorageModuleCellLocation, StorageModuleSide


def build_robot(
    *, weight: float | None, cached_barcode: str | None = None
) -> tuple[Robot, list[str], list[StorageModuleCellLocation]]:
    calls: list[str] = []

    location = StorageModuleCellLocation(
        side=StorageModuleSide.FRONT,
        shelf_idx=0,
        cell_idx=1,
    )
    placeholder_item = CellItem(
        barcode=cached_barcode,
        dimensions=Dimensions(width=20, height=30, length=400),
        data={"name": "calibration test front_module:0:1"},
    )
    cell = SimpleNamespace(item=placeholder_item)
    cleared_locations: list[StorageModuleCellLocation] = []

    robot = Robot.__new__(Robot)
    robot._logger = logging.getLogger("test_robot_retrieve")
    robot._state = SimpleNamespace(current_cell=None)
    robot._operation_context = {}
    robot._last_fault_context = None
    robot._demo_cell_map = None
    robot._grid = SimpleNamespace(
        get_cell=lambda resolved_location: cell,
        get_cell_coordinate=lambda resolved_location: SimpleNamespace(
            to_axis_coordinate=lambda: AxisCoordinate(x=10, y=20, s=0, z_offset=1),
        ),
        clear_item=lambda resolved_location: cleared_locations.append(resolved_location),
    )

    async def io_place(coordinate: AxisCoordinate, nudge_override=None) -> None:
        assert coordinate == AxisCoordinate(x=10, y=20, s=0, z_offset=1)
        assert nudge_override is None
        calls.append("io_place")

    async def get_weight() -> float | None:
        calls.append("get_weight")
        return weight

    robot.io_place = io_place
    robot._io = SimpleNamespace(get_weight=get_weight)
    return robot, calls, cleared_locations


@pytest.mark.asyncio
async def test_retrieve_cancelled_while_weighing_still_clears_grid_cell() -> None:
    awaiting_weight = asyncio.Event()
    robot, _, cleared_locations = build_robot(weight=42.0)
    location = StorageModuleCellLocation(
        side=StorageModuleSide.FRONT,
        shelf_idx=0,
        cell_idx=1,
    )

    async def get_weight() -> float | None:
        awaiting_weight.set()
        await asyncio.Event().wait()  # simulate the 15s stable-weight wait never returning
        return 42.0

    robot._io = SimpleNamespace(get_weight=get_weight)

    task = asyncio.create_task(robot.retrieve_by_location(location.model_dump()))
    await awaiting_weight.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cleared_locations == [location]


@pytest.mark.asyncio
@pytest.mark.parametrize("cycle", ("io_place", "io_pick"))
async def test_io_cycle_homes_the_door_first_and_survives_a_failed_final_home(cycle: str) -> None:
    calls: list[str] = []

    class Tool:
        _my = SimpleNamespace(move_to_position=lambda *_: _record("y_transfer"))
        is_grip_detection_enabled = False

        async def get_param(self, name: str) -> tuple[float, float]:
            return (1.0, 2.0)

        def __getattr__(self, name: str):
            async def step(*_args, **_kwargs) -> None:
                calls.append(name)

            return step

    async def _record(name: str) -> None:
        calls.append(name)

    class Io:
        async def ensure_door_homed(self) -> None:
            calls.append("ensure_door_homed")

        async def open_door(self) -> None:
            calls.append("open_door")

        async def close_door(self) -> None:
            calls.append("close_door")

        async def home_door_after_cycle(self) -> bool:
            calls.append("home_door_after_cycle")
            return False

    robot = Robot.__new__(Robot)
    robot._logger = logging.getLogger("test_io_cycle")
    robot._operation_context = {}
    robot._tool = Tool()
    robot._io = Io()

    await getattr(robot, cycle)(AxisCoordinate(x=10, y=20, s=0, z_offset=1))

    assert calls[0] == "ensure_door_homed"
    assert calls.index("ensure_door_homed") < calls.index("pick")
    assert calls[-2:] == ["home_door_after_cycle", "clear_alarms"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fridge_active", (False, True))
async def test_retrieve_refuses_a_fridge_cell_while_the_fridge_is_disabled(fridge_active: bool) -> None:
    # With demo.fridge.enabled false nothing opens the fridge door, so a normal pick at a
    # configured fridge cell would drive the gripper into it.
    robot, _, _ = build_robot(weight=None)
    fridge_cell = StorageModuleCellLocation(side=StorageModuleSide.FRONT, shelf_idx=0, cell_idx=1)
    placed: list[StorageModuleCellLocation | None] = []
    robot._config = SimpleNamespace(
        demo=SimpleNamespace(
            fridge=SimpleNamespace(
                cells=[SimpleNamespace(location=SimpleNamespace(side="front_module", shelf_idx=0, cell_idx=1))]
            )
        )
    )
    robot._fridge = SimpleNamespace(is_fridge_cell=lambda location: True) if fridge_active else None

    async def io_place(coordinate: AxisCoordinate, nudge_override=None, location=None) -> None:
        placed.append(location)

    robot.io_place = io_place

    result = await robot.retrieve_by_location(fridge_cell.model_dump(), barcode_hint="049000000078")

    if fridge_active:
        assert isinstance(result, RobotCommandSuccessful)
        assert placed == [fridge_cell]
    else:
        assert isinstance(result, RobotCommandError)
        assert "fridge is disabled" in result.error
        assert placed == []


@pytest.mark.asyncio
async def test_direct_hardware_bus_loss_returns_classified_command_error() -> None:
    robot, _, _ = build_robot(weight=None)

    async def move(_coordinate) -> None:
        raise ModbusConnectionError("device 3 unreachable")

    robot._tool = SimpleNamespace(move=move)
    result = await robot.handle_command(MoveCommand(coordinate=AxisCoordinate(x=1, y=2, s=0, z_offset=0)))

    assert isinstance(result, RobotCommandError)
    assert result.fault_code == "motor_bus_disconnected"
    assert result.error == FAULT_MESSAGES["motor_bus_disconnected"]
    assert "device 3" not in result.model_dump_json()
