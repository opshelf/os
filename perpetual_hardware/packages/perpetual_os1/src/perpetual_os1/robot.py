from perpetual_os1.common import AxisCoordinate, ToolCoordinate, load_config_file

from hardware.controller import SharedModbusController as Controller
from perpetual_os1.iobox import IOBox, IOBoxStatus, SizeResult, ItemSizeLocation
from perpetual_os1.tool import Tool, ToolStatus, ToolMotionStatus, ToolItemStatus
from perpetual_os1.storage import (
    StorageGrid, Shelf, Cell, CellStatus, CalibrationCoordinate,
    Dimensions, StorageModuleSide, CellItem, StorageModuleCellLocation
)

from perpetual_os1.exceptions import (
    ConfigError,
)

from perpetual_os1.robot_state import RobotStateManager, RobotStateError

import json
import logging
from pathlib import Path
from enum import Enum
from typing import Any, Dict, Optional, Tuple, Union, Literal, Annotated, List, Callable, TYPE_CHECKING
from pydantic import BaseModel, Field
from dataclasses import dataclass

import time
import asyncio

from PIL import Image

from hardware.printer import ESCImagePrinter as printer
from hardware.printer import AsyncPrinter as aprinter

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
    location: Dict[str, Any]
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
    message: Literal["success"] = "success"

class RobotCommandError(BaseModel):
    error: str

class RobotItemStoredResponse(BaseModel):
    message: Literal["success"] = "success"
    location: StorageModuleCellLocation


class RobotState(BaseModel):
    io: IOBoxStatus
    tool: ToolStatus
    current_cell: Optional[StorageModuleCellLocation] = None

class RobotStatus(BaseModel):
    storage: StorageGrid
    state: RobotState
    light_curtain: bool

RobotResponse = Union[RobotCommandSuccessful, RobotCommandError, SizeResult]


@dataclass
class IOBoxMeasurement:
    dimensions: Optional[Dimensions]
    weight: Optional[float]
    timestamp: float

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
        with open(STORAGE_PATH, 'r') as f:
             data = json.load(f)
        self._grid = StorageGrid.model_validate(data)  # Storage grid configuration

        # Status
        self._cur_item: StorageModuleCellLocation = None # some kind of unique identifier
        self._status: RobotStatus = None

        self._state: RobotState = RobotState(io=IOBoxStatus.IDLE, tool=ToolStatus(motion=ToolMotionStatus.IDLE, item=ToolItemStatus.EMPTY))

        # State Machine
        self._state_manager = RobotStateManager()

        self.p = printer(
            "/dev/usb/lp0",
            print_width_px=384,
            default_darkness=3,       # try 3–4; 5 if you need max
            bytes_per_write=1024,     # slice writes (container-friendly)
            band_height=24,           # required for m=33
            chunk_sleep=0.0,          # 0.01 if PSU is marginal
        )
        self.ap = aprinter(self.p)

    async def configure(self):
        await self._connect_controller("motor", self._motorbus, self._bus_settings["motor"])
        await self._connect_controller("scale", self._scalebus, self._bus_settings["scale"])

        # await self._tool.configure()
        await self._io.configure()

    def get_param(self, param: str):
        try:
            if not hasattr(self._config.robot, param):
                raise KeyError(f"Parameter '{param}' not found")
            return getattr(self._config.robot, param)
        except Exception as e:
            raise ConfigError(f"Failed to get parameter {param}: {str(e)}") from e

    async def io_place(self, coord: AxisCoordinate):
        # Validate state before operation
        self._state_manager.update_state(self._state.tool, self._state.io)
        self._state_manager.validate_operation('place_iobox')

        ppl = await self._tool.get_param("PICKUP_POSITION_L")
        pph = await self._tool.get_param("PICKUP_POSITION_H")

        await self._tool.pick(coord)
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
        self._state_manager.update_state(self._state.tool, self._state.io)

    async def io_pick(self, coord):
        # Validate state before operation
        self._state_manager.update_state(self._state.tool, self._state.io)
        self._state_manager.validate_operation('pick_iobox')

        ppl = await self._tool.get_param("PICKUP_POSITION_L")
        pph = await self._tool.get_param("PICKUP_POSITION_H")

        await self._tool.pick(coord)
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
        self._state_manager.update_state(self._state.tool, self._state.io)

    async def idle(self) -> None:
        await self._tool.move(self._tool.get_param("IDLE_POSITION"))

    async def shutdown(self) -> None:
        await self.stop()
        if self._motorbus:
            await self._motorbus.close()
        if self._scalebus:
            await self._scalebus.close()

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
                    totals = [l + r for l, r in zip(left_vals, right_vals)]
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
                    totals = [l + r for l, r in zip(left_vals, right_vals)]
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
                            status=CellStatus.EMPTY,
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


    async def _collect_iobox_measurement(self) -> IOBoxMeasurement:
        """Gather IOBox measurement data (weight & optical dimensions)."""
        timestamp = time.time()
        weight: Optional[float] = None
        dimensions: Optional[Dimensions] = None

        try:
            weight = await self._io.get_weight()
        except Exception as exc:
            self._logger.warning("IOBox weight read failed", exc_info=exc)

        try:
            dimensions = await self.measure_item_dimensions()
        except Exception as exc:
            self._logger.warning("IOBox dimension measurement failed", exc_info=exc)

        return IOBoxMeasurement(dimensions=dimensions, weight=weight, timestamp=timestamp)


    # plug in sizing from the io box
    async def store(self, item_name: str) -> None:
        """
        Store an item in the storage grid, finding the best available location based on item dimensions.

        Args:
            item_name: Name identifier for the item to store. Item dimensions are
                       obtained via `measure_item_dimensions`.

        Returns:
            RobotResponse indicating success or failure
        """
        if not self._grid:
            return RobotCommandError(error="Storage grid not calibrated")

        # Set default dimensions or use measured ones
        width = 50.0  # default width
        height = 50.0  # default height
        length = 50.0  # default length

        measurement = await self._collect_iobox_measurement()
        weight = measurement.weight
        measured_dimensions = measurement.dimensions
        self._logger.debug(
            "Collected IOBox measurement",
            extra={
                "weight": weight,
                "dimensions": measured_dimensions.model_dump() if measured_dimensions else None,
                "timestamp": measurement.timestamp,
            }
        )

        if measured_dimensions:
            # Add safety margin to ensure item fits (10%)
            width = measured_dimensions.width * 1.1
            height = measured_dimensions.height * 1.1
            length = measured_dimensions.length * 1.1

        # Find a suitable empty location for the item

        item = CellItem(
            dimensions=Dimensions(
                width=width,
                height=height,
                length=length
            ),
            weight=weight,
            data={"name": item_name}  # Store item metadata
        )

        print(measured_dimensions)

        location = self._grid.find_best_slot_for_item(item)
        print(location)

        if not location:
            print("couldn't store")
            return RobotCommandError(error="No suitable storage location found")

        # Get coordinate and store
        coord = self._grid.get_cell_coordinate(location)
        await self.io_pick(coord.to_axis_coordinate())

        # Update storage grid
        self._grid.store_item(location, item)
        self._grid.save()


    async def retrieve(self, item_name: str) -> None:
        """
        Retrieve an item from storage by its name. # todo or upca

        Args:
            item_name: Name identifier of the item to retrieve

        Returns:
            RobotResponse indicating success or failure
        """
        print("retrieving")
        if not self._grid:
            print("not init")
            return RobotCommandError(error="Storage grid not initialized")

        # Find item location
        print("bebdar")
        coord = await self._grid.find_item_location(item_name=item_name)
        print(coord)

        if not coord:
            print("couldn't find")
            return RobotCommandError(error=f"Item '{item_name}' not found in storage")

        # Retrieve item
        # await self._tool.move(coord)
        await self.io_place(coord)

        # clear the item we just retrieved
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
        if location:

            self._grid.clear_item(location)
            self._grid.save()

    async def measure_item_dimensions(self, n_frames: int = 5) -> Optional[Dimensions]:
        """
        Use the IOBox's AFSCam to measure the dimensions of an item currently in the IO box.
        Returns the aggregate dimensions including footprint and depth measurements.

        Args:
            n_frames: Number of frames to capture for measurement (default=5)

        Returns:
            Dimensions object (length, width, height) in millimeters.
            Returns None if measurement fails or required values are missing.

        Raises:
            RobotStateError: If there is no item in the IOBox to measure
        """
        # Validate state before operation -> change state on weight read
        # self._state_manager.update_state(self._state.tool, self._state.io)
        # self._state_manager.validate_operation('measure')
        if not self._io or not self._io._cam:
            raise RuntimeError("IOBox camera not configured")

        # Take measurement using AFSCam
        measure_result = await self._io.measure_item()
        # print(measure_result)

        if not measure_result["ok"]:
            return None

        # Extract relevant measurements from aggregates
        agg = measure_result["aggregate"]
        if not agg:
            return None

        # Prefer footprint measurements (from pinhole model) and fall back to scale data if needed.
        width = agg.get("footprint_major_mm_med") or agg.get("scale_major_mm_med")
        height = agg.get("footprint_minor_mm_med") or agg.get("scale_minor_mm_med")
        length = agg.get("depth_mm_med")/10

        if width is None or height is None or length is None:
            return None

        return Dimensions(length=float(length), width=float(width), height=float(height))

    async def get_status(self) -> RobotStatus:
        status = RobotStatus(storage=self._grid,
                             state=RobotState(io=self._io._status,
                                              tool=self._tool._status),
                             light_curtain= False)
        self._status = status
        return status
    
    async def test(self, coordinate) -> None:
        # await self.ap.print_image("barcode.png")
        print
        # self._io._printer.print_text("hi beb")
        # self._io._printer.print_raster()
        # await self.io_place(AxisCoordinate(917, 381))
        # await self.store(item_name="andi")
        # await self.retrieve(item_name="andi")
        # await self.auto_calibrate_storage()

    async def handle_command(self, command: RobotCommands):
        if isinstance(command, StopCommand):
            await self.stop()
            return RobotCommandSuccessful()
        elif isinstance(command, InitializeCommand):
            await self.configure()
            return RobotCommandSuccessful()
        elif isinstance(command, HomeCommand):
            await self._tool.home()
            return RobotCommandSuccessful()
        elif isinstance(command, MoveCommand):
            # await self._tool.move(command.coordinate)
            await self.test(command.coordinate)
            return RobotCommandSuccessful()
        elif isinstance(command, IdleCommand):
            await self.idle() # todo remove
            return RobotCommandSuccessful()
        elif isinstance(command, StoreItemCommand):
            await self.store(command.item)
            return RobotCommandSuccessful()
        elif isinstance(command, RetrieveItemCommand):
            await self.retrieve(command.item)
            return RobotCommandSuccessful()
        else:
            return RobotCommandError(error="Invalid Robot Command")
