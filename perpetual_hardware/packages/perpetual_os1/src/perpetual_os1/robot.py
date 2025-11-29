from perpetual_os1.common import AxisCoordinate, ToolCoordinate, load_config_file

from hardware.controller import SharedModbusController as Controller
from perpetual_os1.iobox import IOBox
from perpetual_os1.tool import Tool
from perpetual_os1.storage import (
    StorageGrid, Shelf, Cell, CellStatus, CalibrationCoordinate,
    Dimensions, StorageModuleSide, CellItem, StorageModuleCellLocation
)

from perpetual_os1.exceptions import (
    ConfigError,
)

from perpetual_os1.robot_state import RobotStateManager

import json
import logging
import traceback
from pathlib import Path
from typing import Any, Dict, Optional, Union, Literal, Annotated, List, TYPE_CHECKING
from pydantic import BaseModel, Field

import asyncio
import sentry_sdk


if TYPE_CHECKING:
    from hardware.motor import Motor

# from hardware.printer import AsyncPrinter as printer

import os
STORAGE_PATH = os.path.join(os.path.dirname(__file__), 'storage.json')
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

class StopCommand(BaseModel):
    command: Literal["stop"] = "stop"

class InitializeCommand(BaseModel):
    command: Literal["initialize"] = "initialize"

class HomeCommand(BaseModel):
    command: Literal["home"] = "home"

class MoveCommand(BaseModel):
    command: Literal["move"] = "move"
    coordinate: AxisCoordinate

class MoveToolCommand(BaseModel):
    command: Literal["movetool"] = "movetool"
    coordinate: ToolCoordinate

class IdleCommand(BaseModel):
    command: Literal["idle"] = "idle"

class RetrieveItemCommand(BaseModel):
    command: Literal["retrieve_item"] = "retrieve_item"
    location: Dict[str, Any]

class StoreItemCommand(BaseModel):
    command: Literal["store_item"] = "store_item"
    # location: Dict[str, Any]
    item: Optional[Dict[str, Any]] = None

RobotCommands = Annotated[
    Union[
        StopCommand,
        InitializeCommand,
        HomeCommand,
        MoveCommand,
        MoveToolCommand,
        IdleCommand,
        RetrieveItemCommand,
        StoreItemCommand,
    ],
    Field(discriminator="command"),
]

# Define response types
class RobotCommandSuccessful(BaseModel):
    command: str = "unknown"
    message: Literal["success"] = "success"
    location: Optional[StorageModuleCellLocation] = None
    upca: Optional[str] = None


class RobotCommandError(BaseModel):
    command: str = "unknown"
    error: str


class RobotBarcodeResponse(BaseModel):
    message: Literal["success"] = "success"
    upca: str


# class RobotState(BaseModel):
#     # io: IOBoxStatus
#     # tool: ToolStatus
#     current_cell: Optional[StorageModuleCellLocation] = None


class StatusFlags(BaseModel):
    value: int = 0
    stall_guard2_status: bool = False
    over_temperature: bool = False
    over_temperature_pre_warning: bool = False
    short_to_ground_a: bool = False
    short_to_ground_b: bool = False
    open_load_a: bool = False
    open_load_b: bool = False
    stand_still: bool = False


class TrinamicErrorFlags(BaseModel):
    value: int = 0
    stall_guard_error: bool = False
    deviation_error: bool = False


class AxisTelemetry(BaseModel):
    status_flags: StatusFlags = Field(default_factory=StatusFlags)
    error_flags: TrinamicErrorFlags = Field(default_factory=TrinamicErrorFlags)
    position: float = 0.0
    homed: int = 0
    hard_stopped: int = 0


class ToolAxisStatus(BaseModel):
    z_axis: AxisTelemetry


class GantryStatus(BaseModel):
    x_axis: AxisTelemetry
    y_axis: AxisTelemetry


class RobotState(BaseModel):
    current_cell: Optional[StorageModuleCellLocation] = None


class RobotStatus(BaseModel):
    gantry: GantryStatus
    tool: ToolAxisStatus
    state: RobotState
    storage: StorageGrid

RobotResponse = Union[RobotCommandSuccessful, RobotCommandError, RobotBarcodeResponse]

class Robot:
    def __init__(self = None) -> None:
        # Load State / Configuration
        self._config = load_config_file(CONFIG_PATH)
        self._bus_settings = self._prepare_bus_settings()
        self._logger = logging.getLogger(__name__).getChild("Robot")

        motor_bus = self._bus_settings["motor"]
        scale_bus = self._bus_settings["scale"]
        self._motorbus = Controller(
            port=motor_bus["ports"][0],
            baudrate=motor_bus["baudrate"],
            parity=motor_bus["parity"],
            timeout=motor_bus["timeout"],
        )
        self._scalebus = Controller(
            port=scale_bus["ports"][0],
            baudrate=scale_bus["baudrate"],
            parity=scale_bus["parity"],
            timeout=scale_bus["timeout"],
        )

        tool_params = self._config.get("robot.tool", {})
        io_params = self._config.get("robot.io", {})

        # Robot Components
        self._tool = Tool(motorcontroller=self._motorbus, params=tool_params)
        self._io = IOBox(motorcontroller=self._motorbus, scalecontroller=self._scalebus, params=io_params)

        # storage grid to store physical robot state
        if os.path.exists(STORAGE_PATH) is True:
            try:
                with open(STORAGE_PATH, 'r') as f:
                    data = json.load(f)
                self._grid = StorageGrid.model_validate(data)
            except (json.JSONDecodeError, Exception) as e:
                self._logger.warning("storage_load_failed", path=STORAGE_PATH, error=str(e))
                self._grid = StorageGrid()
        else:
            self._logger.info("storage_file_missing", path=STORAGE_PATH, action="using_empty_grid")
            self._grid = StorageGrid()

        # Status
        self._cur_item: StorageModuleCellLocation = None # some kind of unique identifier
        self._status: RobotStatus = None

        # self._state: RobotState = RobotState(io=IOBoxStatus.IDLE, tool=ToolStatus(motion=ToolMotionStatus.IDLE, item=ToolItemStatus.EMPTY), )
        self._state: RobotState = RobotState(current_cell=None)

        # State Machine
        self._state_manager = RobotStateManager()


    async def configure(self):
        await self._connect_controller("motor", self._motorbus, self._bus_settings["motor"])
        await self._connect_controller("scale", self._scalebus, self._bus_settings["scale"])    
        await self._tool.configure()
        await self._io.configure()

    def get_param(self, param: str):
        try:
            if hasattr(self._config.robot, param):
                return getattr(self._config.robot, param)
            return self._config.robot.grid.get_param(param)
        except Exception as e:
            raise ConfigError(f"Failed to get parameter {param}: {str(e)}") from e

    async def io_place(self, coord: AxisCoordinate):
        # Validate state before operation
        # self._state_manager.update_state(self._state.tool, self._state.io)
        # self._state_manager.validate_operation('place_iobox')

        ppl = await self._tool.get_param("PICKUP_POSITION_L")
        pph = await self._tool.get_param("PICKUP_POSITION_H")

        await self._tool.pick(coord)
        await self._tool.rotate_to_position("front")
        await self._tool.move(AxisCoordinate(ppl[0], ppl[1])) # move to safe height
        await self._io.open_door()
        await self._tool.extend_IO()
        await self._tool.move(AxisCoordinate(pph[0], pph[1]))
        await self._tool.retract()
        await self._io.close_door()
        await self._tool.move(coord)
        await self._tool.place(coord)

        await self._io._md.home()
        await self._tool.clear_alarms()

        # Update state after operation
        # self._state_manager.update_state(self._state.tool, self._state.io)

    async def io_pick(self, coord):
        # Validate state before operation
        # self._state_manager.update_state(self._state.tool, self._state.io)
        # self._state_manager.validate_operation('pick_iobox')

        ppl = await self._tool.get_param("PICKUP_POSITION_L")
        pph = await self._tool.get_param("PICKUP_POSITION_H")

        await self._tool.pick(coord)
        await self._tool.rotate_to_position("front")
        await self._tool.move(AxisCoordinate(pph[0], pph[1])) # move to safe height
        await self._io.open_door()
        await self._tool.extend()
        await self._tool.move(AxisCoordinate(ppl[0], ppl[1])) # move to safe height
        await self._tool.retract()
        await self._io.close_door()
        await self._tool.move(coord)
        await self._tool.place(coord)

        await self._io._md.home() # clear alarms io
        await self._tool.clear_alarms()

        # Update state after operation
        # self._state_manager.update_state(self._state.tool, self._state.io)

    async def idle(self) -> None:
        await self._tool.move(self._tool.get_param("IDLE_POSITION"))

    async def shutdown(self) -> None:
        await self.stop()
        if self._motorbus:
            await self._motorbus.close()
        if self._scalebus:
            await self._scalebus.close()

    async def print_random_barcode(
        self,
        *,
        height: int = 96,
        width: int = 3,
        hri: str = "none",
        feed_after: int = 2,
    ) -> RobotResponse:
        """
        Ask the IOBox printer to generate and print a random UPC-A barcode.

        Returns:
            RobotBarcodeResponse on success or RobotCommandError on failure.
        """
        if self._io is None:
            return RobotCommandError(error="IOBox not configured")
        try:
            upca = self._io.print_random_barcode(
                height=height,
                width=width,
                hri=hri,
                feed_after=feed_after,
            )
        except IOError as exc:
            return RobotCommandError(error=str(exc))
        return RobotBarcodeResponse(upca=upca)


    async def stop(self) -> None:
        await self._tool.stop()
        await self._io.stop()

    def _prepare_bus_settings(self) -> Dict[str, Dict[str, Any]]:
        defaults = {
            "motor": {
                "ports": ["/dev/ttyUSB1"],
                "baudrate": 115200,
                "parity": "N",
                "timeout": 2.0,
                "retry_delay_s": 0.25,
            },
            "scale": {
                "ports": ["/dev/ttyUSB0"],
                "baudrate": 9600,
                "parity": "N",
                "timeout": 2.0,
                "retry_delay_s": 0.25,
            },
        }

        bus_cfg = getattr(self._config.robot, "bus", None)

        def build(name: str) -> Dict[str, Any]:
            cfg = getattr(bus_cfg, name, None) if bus_cfg else None
            settings = defaults[name].copy()
            if cfg is not None:
                settings.update(
                    {
                        "ports": list(cfg.ports),
                        "baudrate": cfg.baudrate,
                        "parity": cfg.parity,
                        "timeout": cfg.timeout,
                        "retry_delay_s": cfg.retry_delay_s,
                    }
                )
            return settings

        return {"motor": build("motor"), "scale": build("scale")}

    async def _connect_controller(self, label: str, controller: Controller, settings: Dict[str, Any]) -> None:
        errors: List[str] = []
        for port in settings["ports"]:
            controller.port = port
            controller.baudrate = settings["baudrate"]
            controller.parity = settings["parity"]
            controller.timeout = settings["timeout"]
            try:
                await controller.connect()
            except Exception as exc:
                errors.append(f"{port}: {exc}")
                await controller.close()
            else:
                if controller.client and controller.client.connected:
                    return
                errors.append(f"{port}: client not connected")
                await controller.close()

            delay = settings.get("retry_delay_s", 0)
            if delay:
                await asyncio.sleep(delay)

        detail = "; ".join(errors) if errors else "no ports configured"
        raise ConfigError(f"{label.capitalize()} bus connection failed ({detail})")

    async def auto_calibrate_storage(self, side: Union[StorageModuleSide, str]) -> RobotResponse:
        """Calibrate storage for a single module side (LEFT or RIGHT)."""
        try:
            if isinstance(side, str):
                try:
                    side = StorageModuleSide(side)
                except ValueError:
                    return RobotCommandError(error=f"Invalid storage side '{side}'")

            shelf_y_positions = await self._tool.get_shelves()
            if not shelf_y_positions:
                return RobotCommandError(error="No shelves detected")

            num_shelves = len(shelf_y_positions)

            raw_cells_per_shelf = self.get_param("CELLS_PER_SHELF")
            raw_cell_spacings = self.get_param("CELL_SPACINGS")
            raw_cell_heights = self.get_param("CELL_HEIGHTS")
            cell_widths = self.get_param("CELL_WIDTHS")

            if any(param is None for param in [raw_cells_per_shelf, raw_cell_spacings, raw_cell_heights, cell_widths]):
                return RobotCommandError(error="Missing required parameters: CELLS_PER_SHELF, CELL_SPACINGS, CELL_HEIGHTS, or CELL_WIDTHS")

            def _expand_per_shelf(value, name, caster):
                if value is None:
                    raise RobotCommandError(error=f"{name} is not defined")
                if isinstance(value, list):
                    if len(value) != num_shelves:
                        raise RobotCommandError(
                            error=f"{name} length ({len(value)}) does not match detected shelves ({num_shelves})"
                        )
                    return [caster(x) for x in value]
                return [caster(value)] * num_shelves

            def _normalize_cells_per_shelf(raw_value):
                if isinstance(raw_value, dict):
                    left_vals = _expand_per_shelf(raw_value.get("left"), "CELLS_PER_SHELF.left", int)
                    right_vals = _expand_per_shelf(raw_value.get("right"), "CELLS_PER_SHELF.right", int)
                    totals = [left + right for left, right in zip(left_vals, right_vals)]
                    return totals, list(zip(left_vals, right_vals))

                if isinstance(raw_value, list) and raw_value and all(isinstance(entry, (list, tuple)) for entry in raw_value):
                    if len(raw_value) != num_shelves:
                        raise RobotCommandError(
                            error=f"CELLS_PER_SHELF length ({len(raw_value)}) does not match detected shelves ({num_shelves})"
                        )
                    left_vals = []
                    right_vals = []
                    for entry in raw_value:
                        if len(entry) != 2:
                            raise RobotCommandError(error="Each CELLS_PER_SHELF entry must have [left, right] counts")
                        left_vals.append(int(entry[0]))
                        right_vals.append(int(entry[1]))
                    totals = [left + right for left, right in zip(left_vals, right_vals)]
                    return totals, list(zip(left_vals, right_vals))

                totals = _expand_per_shelf(raw_value, "CELLS_PER_SHELF", int)
                left_vals = [total // 2 for total in totals]
                right_vals = [total - left for total, left in zip(totals, left_vals)]
                return totals, list(zip(left_vals, right_vals))

            try:
                cells_per_shelf, per_side_counts = _normalize_cells_per_shelf(raw_cells_per_shelf)
                cell_spacings = _expand_per_shelf(raw_cell_spacings, "CELL_SPACINGS", float)
                cell_heights = _expand_per_shelf(raw_cell_heights, "CELL_HEIGHTS", float)
            except RobotCommandError as exc:
                return exc
            except Exception as exc:
                return RobotCommandError(error=str(exc))

            x_anchor_points = await self._tool.update_grid(shelf_y_positions)
            if not x_anchor_points or len(x_anchor_points) != num_shelves:
                return RobotCommandError(
                    error=f"Anchor detection returned {len(x_anchor_points) if x_anchor_points is not None else 0} anchors; expected {num_shelves}"
                )

            cell_positions = await self._grid.calculate_storage_positions(
                shelf_y_positions=shelf_y_positions,
                cells_per_shelf=cells_per_shelf,
                cell_spacings=cell_spacings,
                x_anchor_points=x_anchor_points,
            )

            def _cell_width_for_index(index: int) -> float:
                if isinstance(cell_widths, list):
                    if index >= len(cell_widths):
                        raise RobotCommandError(error="CELL_WIDTHS list is shorter than total cell count")
                    return float(cell_widths[index])
                return float(cell_widths)

            target_shelves: List[Shelf] = []
            z_offset = 0.0

            for shelf_idx, shelf_cells in enumerate(cell_positions):
                left_count, right_count = per_side_counts[shelf_idx]
                if left_count + right_count != len(shelf_cells):
                    return RobotCommandError(
                        error=(
                            f"Side cell counts ({left_count}+{right_count}) do not match total cells "
                            f"({len(shelf_cells)}) for shelf {shelf_idx}"
                        )
                    )

                if side == StorageModuleSide.LEFT:
                    relevant_cells = shelf_cells[:left_count]
                    index_offset = 0
                else:
                    relevant_cells = shelf_cells[left_count:left_count + right_count]
                    index_offset = left_count

                shelf_height = cell_heights[shelf_idx]
                shelf_cell_models: List[Cell] = []
                for offset, coord in enumerate(relevant_cells, start=index_offset):
                    shelf_cell_models.append(
                        Cell(
                            status=CellStatus.AVAILABLE,
                            item=None,
                            calibration_point=CalibrationCoordinate(x=coord.x, y=coord.y, z=z_offset),
                            dimensions=Dimensions(width=_cell_width_for_index(offset), height=shelf_height, length=400.0)
                        )
                    )

                if shelf_cell_models:
                    target_shelves.append(
                        Shelf(
                            shelf_height=shelf_height,
                            shelf_width=sum(cell.dimensions.width for cell in shelf_cell_models),
                            cells=shelf_cell_models
                        )
                    )

            current_left = [] if self._grid is None else self._grid.left_module
            current_right = [] if self._grid is None else self._grid.right_module

            if side == StorageModuleSide.LEFT:
                left_module = target_shelves
                right_module = current_right
            else:
                left_module = current_left
                right_module = target_shelves

            storage_grid = StorageGrid(
                left_module=left_module,
                right_module=right_module,
                filename=str(CONFIG_PATH.parent / "storage_grid.json")
            )
            storage_grid.save()
            self._grid = storage_grid

            return RobotCommandSuccessful()

        except Exception as e:
            return RobotCommandError(error=f"Storage calibration failed: {str(e)}")


    # plug in sizing from the io box
    async def store(self, item_payload: Any) -> RobotResponse:
        """
        Store an item in the storage grid, finding the best available location based on item dimensions.

        Args:
            item_name: Name identifier for the item to store. Item dimensions are
                       obtained via `measure_item_dimensions`.

        Returns:
            RobotResponse indicating success or failure
        """

        # # # Poll IOBox to refresh presence state before storing
        # await self._io.detect_item_presence()

        # # Validate state before storing
        # self._state = RobotState(io=self._io._status, tool=self._tool._status)
        # self._state_manager.update_state(self._state.tool, self._state.io)
        # try:
        #     self._state_manager.validate_operation('store')
        # except RobotStateError as exc:
        #     print("error")
        #     return RobotCommandError(error=str(exc))

        command_name = "store_item"

        if not self._grid:
            return RobotCommandError(command=command_name, error="Storage grid not calibrated")

        resolved_upca: str
        resolved_item_name: str
        if isinstance(item_payload, str):
            resolved_upca = item_payload
            resolved_item_name = item_payload
        elif isinstance(item_payload, dict):
            resolved_upca = item_payload.get("upca") or item_payload.get("name") or "item"
            resolved_item_name = item_payload.get("name") or resolved_upca
        else:
            resolved_upca = "item"
            resolved_item_name = "item"

        provided_dimensions = None
        if isinstance(item_payload, dict):
            provided_dimensions = item_payload.get("dimensions")

        if provided_dimensions is None:
            return RobotCommandError(command=command_name, error="Item dimensions required")

        self._logger.info("Using provided dimensions: %s", provided_dimensions)
        width = float(provided_dimensions.get("width", 0))
        height = float(provided_dimensions.get("height", 0))
        length = float(provided_dimensions.get("length", 0))

        if width <= 0 or height <= 0 or length <= 0:
            return RobotCommandError(command=command_name, error="Invalid dimensions provided")

        weight = await self._io.get_weight()

        # Find a suitable empty location for the item

        item = CellItem(
            dimensions=Dimensions(
                width=width,
                height=height,
                length=length
            ),
            weight=weight,
            data={"name": resolved_item_name},  # Store item metadata
            upca=resolved_upca
        )

        location = self._grid.find_best_slot_for_item(item)

        if not location:
            return RobotCommandError(command=command_name, error="No suitable storage location found")

        self._state.current_cell = location
        try:
            coord = self._grid.get_cell_coordinate(location)
            await self.io_pick(coord.to_axis_coordinate())

            # Update storage grid
            self._grid.store_item(location, item)
            self._grid.save()
            return RobotCommandSuccessful(command=command_name, location=location, upca=resolved_upca)
        except Exception as exc:
            self._logger.exception("Store command failed for %s", resolved_item_name)
            sentry_sdk.capture_exception(exc)
            return RobotCommandError(
                command=command_name,
                error=f"{type(exc).__name__}: {exc}"
            )



    async def retrieve(self, item_name: str) -> RobotResponse:
        """
        Retrieve an item from storage by its name. # todo or upca

        Args:
            item_name: Name identifier of the item to retrieve

        Returns:
            RobotResponse indicating success or failure
        """
        # # Validate state before retrieval
        # self._state = RobotState(io=self._io._status, tool=self._tool._status)
        # self._state_manager.update_state(self._state.tool, self._state.io)
        # try:
        #     self._state_manager.validate_operation('retrieve')
        # except RobotStateError as exc:
        #     return RobotCommandError(error=str(exc))

        command_name = "retrieve_item"

        if not self._grid:
            return RobotCommandError(command=command_name, error="Storage grid not initialized")

        location = next(
            (
                entry.location
                for entry in self._grid.get_all_cell_data()
                if entry.cell
                and entry.cell.data
                and isinstance(entry.cell.data, dict)
                and entry.cell.data.get("name") == item_name
            ),
            None,
        )
        if location is None:
            return RobotCommandError(command=command_name, error=f"Item '{item_name}' not found in storage")

        return await self.retrieve_by_location(location.model_dump())

    async def _build_axis_status(self, motor: Optional["Motor"]) -> AxisTelemetry:
        position = 0.0
        if motor is None:
            return AxisTelemetry(position=position)
        try:
            position = await motor.get_position()
        except Exception as exc:
            self._logger.debug("Failed to read motor position: %s", exc)
        return AxisTelemetry(position=position)

    async def get_status(self) -> RobotStatus:
        gantry_status = GantryStatus(
            x_axis=await self._build_axis_status(getattr(self._tool, "_mx", None)),
            y_axis=await self._build_axis_status(getattr(self._tool, "_my", None)),
        )
        tool_status = ToolAxisStatus(
            z_axis=await self._build_axis_status(getattr(self._tool, "_mg", None))
        )
        state = RobotState(current_cell=self._state.current_cell)
        status = RobotStatus(
            gantry=gantry_status,
            tool=tool_status,
            state=state,
            storage=self._grid,
        )
        self._status = status
        return status
    

    async def teststore(self) -> None:
        await self.store("beb")
        # await self.retrieve("beb")

    async def testretrieve(self) -> None:
        # await self.store("beb")
        await self.retrieve("beb")

    async def testone(self, coordinate) -> None:
        """Exercise pick/place at every calibrated left-module location."""

        if not self._grid.left_module:
            self._logger.warning("No left-module shelves available for storage test")
            return

        for shelf_idx, shelf in enumerate(self._grid.left_module):
            for cell_idx, cell in enumerate(shelf.cells):
                calibration_point = cell.calibration_point
                if calibration_point is None:
                    self._logger.warning(
                        "Skipping left-module shelf %s cell %s: missing calibration point",
                        shelf_idx,
                        cell_idx,
                    )
                    continue

                coordinate = calibration_point.to_axis_coordinate()
                try:
                    await self._tool.pick(coordinate)
                    await self._tool.place(coordinate)
                except Exception:
                    self._logger.exception(
                        "Pick/place sequence failed at left-module shelf %s cell %s",
                        shelf_idx,
                        cell_idx,
                    )
                    raise


    async def handle_command(self, command: RobotCommands) -> RobotResponse:
        command_name = getattr(command, "command", "unknown")
        try:
            if isinstance(command, StopCommand):
                await self.stop()
                return RobotCommandSuccessful(command=command_name)
            elif isinstance(command, InitializeCommand):
                await self._tool.configure()
                return RobotCommandSuccessful(command=command_name)
            elif isinstance(command, HomeCommand):
                await self.print_random_barcode()
                return RobotCommandSuccessful(command=command_name)
            elif isinstance(command, MoveCommand):
                # await self._tool.move(command.coordinate)
                await self.teststore()
                return RobotCommandSuccessful(command=command_name)
            elif isinstance(command, IdleCommand):
                await self.testretrieve() # todo remove
                return RobotCommandSuccessful(command=command_name)
            elif isinstance(command, StoreItemCommand):
                payload = getattr(command, "upca", None)
                if payload is None:
                    payload = getattr(command, "item", None)
                return await self.store(payload)
            elif isinstance(command, RetrieveItemCommand):
                return await self.retrieve_by_location(command.location)
            else:
                return RobotCommandError(command=command_name, error="Invalid Robot Command")
        except Exception as exc:
            self._logger.exception("Unhandled error while processing command %s", command_name)
            sentry_sdk.capture_exception(exc)
            return RobotCommandError(
                command=command_name,
                error=f"{type(exc).__name__}: {exc}"
            )

    async def retrieve_by_location(self, location_payload: Dict[str, Any]) -> RobotResponse:
        """
        Retrieve an item from a specific storage location.
        """
        command_name = "retrieve_item"

        if not self._grid:
            return RobotCommandError(command=command_name, error="Storage grid not initialized")

        try:
            location = StorageModuleCellLocation(**location_payload)
        except Exception:
            return RobotCommandError(command=command_name, error="Invalid location payload")

        try:
            cell = self._grid.get_cell(location)
        except Exception:
            return RobotCommandError(command=command_name, error="Location out of range")

        if cell.status != CellStatus.OCCUPIED or cell.item is None:
            return RobotCommandError(
                command=command_name,
                error=f"No item found at location (shelf: {location.shelf_idx}, cell: {location.cell_idx})"
            )

        self._state.current_cell = location
        retrieved_upca = cell.item.upca or (cell.item.data.get("upca") if cell.item.data else None)

        try:
            coord = self._grid.get_cell_coordinate(location).to_axis_coordinate()
            await self.io_place(coord)
            self._grid.clear_item(location)
            self._grid.save()
            return RobotCommandSuccessful(command=command_name, location=location, upca=retrieved_upca)
        except Exception as exc:
            self._logger.exception(
                "Retrieve command failed at shelf %s cell %s",
                location.shelf_idx,
                location.cell_idx,
            )
            sentry_sdk.capture_exception(exc)
            return RobotCommandError(
                command=command_name,
                error=f"{type(exc).__name__}: {exc}"
            )
