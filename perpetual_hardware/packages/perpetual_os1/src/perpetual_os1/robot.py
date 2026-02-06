import json as _json
from pathlib import Path as _Path

from perpetual_os1.common import AxisCoordinate, ToolCoordinate, load_config_file, CONFIG_PATH, resolve_grid_path

from hardware.controller import SharedModbusController as Controller
from perpetual_os1.iobox import IOBox
from perpetual_os1.tool import Tool
from perpetual_os1.storage import (
    StorageGrid, Shelf, Cell, CellStatus, CalibrationCoordinate,
    Dimensions, StorageModuleSide, CellItem, StorageModuleCellLocation,
    StorageValidationError,
)

from perpetual_os1.common import ConfigError
from perpetual_os1.exceptions import MotorError, MovementError, ToolError


class RobotConfigError(Exception):
    """Exception raised for robot configuration errors."""
    pass

from perpetual_os1.robot_state import RobotStateManager  # noqa: E402

import logging  # noqa: E402
from typing import Any, Dict, Optional, Union, Literal, Annotated, List, TYPE_CHECKING  # noqa: E402
from pydantic import BaseModel, Field, model_validator  # noqa: E402

import asyncio  # noqa: E402
import sentry_sdk  # noqa: E402


if TYPE_CHECKING:
    from hardware.motor import Motor


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
    upca: Optional[str] = None
    location: Optional[Dict[str, Any]] = None

class StoreItemCommand(BaseModel):
    command: Literal["store_item"] = "store_item"
    item: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_item_data(self) -> "StoreItemCommand":
        if self.item is None or len(self.item) == 0:
            raise ValueError("item field is required with 'upca' or 'dimensions'")

        has_upca = self.item.get("upca") is not None
        has_dimensions = self.item.get("dimensions") is not None

        if has_upca is False and has_dimensions is False:
            raise ValueError("item must contain 'upca' or 'dimensions'")

        return self

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


class RobotCommandSuccessful(BaseModel):
    command: str = "unknown"
    message: Literal["success"] = "success"
    location: Optional[StorageModuleCellLocation] = None
    upca: Optional[str] = None
    weight: Optional[float] = None


class RobotCommandError(BaseModel):
    command: str = "unknown"
    error: str


class RobotBarcodeResponse(BaseModel):
    message: Literal["success"] = "success"
    upca: str



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
    curtain_blocked: Optional[bool] = None
    door_open: Optional[bool] = None
    curtain_guard_active: Optional[bool] = None
    awaiting_curtain_ack: bool = False

RobotResponse = Union[RobotCommandSuccessful, RobotCommandError, RobotBarcodeResponse]

class Robot:
    def __init__(self) -> None:
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

        from hardware.motor import CurtainSafety
        self._curtain_safety = CurtainSafety()
        self._tool = Tool(motorcontroller=self._motorbus, params=tool_params, safety=self._curtain_safety)
        self._io = IOBox(motorcontroller=self._motorbus, scalecontroller=self._scalebus, params=io_params, safety=self._curtain_safety)

        self._grid_path = resolve_grid_path(self._config)

        try:
            self._grid = StorageGrid.load_from_file(self._grid_path)
        except StorageValidationError as e:
            self._logger.warning("storage_load_failed: %s - %s", self._grid_path, str(e))
            self._grid = StorageGrid()

        self._cur_item: StorageModuleCellLocation | None = None
        self._status: RobotStatus | None = None

        self._state: RobotState = RobotState(current_cell=None)
        self._state_manager = RobotStateManager()
        self._motor_time_accumulator: float = 0.0

        self._demo_cell_map: dict[str, StorageModuleCellLocation] | None = None
        self._demo_nudge_map: dict[tuple[str, int, int], float] = {}
        if self._config.demo.enabled is True:
            self._demo_cell_map = self._load_demo_cell_map(self._config.demo.cell_map_path)


    @staticmethod
    def _parse_demo_location(value: str) -> StorageModuleCellLocation:
        side_map = {
            "L": StorageModuleSide.LEFT,
            "R": StorageModuleSide.RIGHT,
            "LEFT_MODULE": StorageModuleSide.LEFT,
            "RIGHT_MODULE": StorageModuleSide.RIGHT,
        }
        parts = value.split(":")
        if len(parts) != 3:
            raise ValueError(f"invalid demo location format: {value!r}")

        side_key = parts[0].strip().upper()
        if side_key not in side_map:
            raise ValueError(f"invalid demo location format: {value!r}")

        return StorageModuleCellLocation(
            side=side_map[side_key],
            shelf_idx=int(parts[1]),
            cell_idx=int(parts[2]),
        )

    def _load_demo_cell_map(self, path: str) -> dict[str, StorageModuleCellLocation] | None:
        file_path = _Path(path)
        if file_path.exists() is False:
            self._logger.warning("demo_cell_map_missing: %s", path)
            return None

        raw = _json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) is False:
            raise ValueError("demo mapping must be a JSON object")

        self._demo_nudge_map = {}
        cell_map: dict[str, StorageModuleCellLocation] = {}

        legacy_mapping = raw.get("code_cell_mapping")
        if isinstance(legacy_mapping, dict):
            for barcode, loc_str in legacy_mapping.items():
                if isinstance(loc_str, str) is False:
                    raise ValueError(f"invalid legacy demo cell mapping for {barcode!r}")
                cell_map[str(barcode)] = self._parse_demo_location(loc_str)

            nudge_raw = raw.get("nudge_override_mm", {})
            if isinstance(nudge_raw, dict) is False:
                raise ValueError("legacy nudge_override_mm must be an object")
            for loc_str, nudge_mm in nudge_raw.items():
                if isinstance(loc_str, str) is False:
                    raise ValueError("legacy nudge_override_mm keys must be location strings")
                loc = self._parse_demo_location(loc_str)
                self._demo_nudge_map[(loc.side.value, loc.shelf_idx, loc.cell_idx)] = float(nudge_mm)
        else:
            for barcode, payload in raw.items():
                if isinstance(payload, str):
                    cell_map[str(barcode)] = self._parse_demo_location(payload)
                    continue

                if isinstance(payload, dict) is False:
                    raise ValueError(f"invalid demo entry payload for {barcode!r}")

                cell_value = payload.get("cell")
                if isinstance(cell_value, str) is False:
                    raise ValueError(f"demo entry for {barcode!r} must include string field 'cell'")

                location = self._parse_demo_location(cell_value)
                cell_map[str(barcode)] = location

                nudge_mm = payload.get("nudge_override_mm")
                if nudge_mm is not None:
                    self._demo_nudge_map[(location.side.value, location.shelf_idx, location.cell_idx)] = float(nudge_mm)

        self._logger.info(
            "demo_cell_map_loaded: %d entries, %d nudge overrides from %s",
            len(cell_map),
            len(self._demo_nudge_map),
            path,
        )
        return cell_map

    def _get_demo_nudge(self, location: StorageModuleCellLocation) -> float | None:
        return self._demo_nudge_map.get(
            (location.side.value, location.shelf_idx, location.cell_idx)
        )

    def _did_complete_motor_move(self, elapsed: float) -> None:
        self._motor_time_accumulator += elapsed

    def drain_motor_time(self) -> float:
        value = self._motor_time_accumulator
        self._motor_time_accumulator = 0.0
        return value

    async def configure(self):
        await self._connect_controller("motor", self._motorbus, self._bus_settings["motor"])

        io_scale_config = self._config.get("robot.io.scale", None)
        scale_enabled = getattr(io_scale_config, "enabled", True) if io_scale_config is not None else True

        if scale_enabled is True:
            await self._connect_controller("scale", self._scalebus, self._bus_settings["scale"])
        else:
            self._logger.info("scale_bus_skipped: scale is disabled in config")

        await self._tool.configure()
        await self._io.configure()

        for name in ["x_axis", "y_axis", "z_axis", "stage", "rotate", "door"]:
            motor = self._get_motor_by_name(name)
            if motor is not None:
                motor._on_move_complete = self._did_complete_motor_move

    def sync_occupied_cells(self, occupied: list[tuple[str, int, int]]) -> None:
        if self._grid is None:
            return

        self._grid.clear_all_items()

        occupied_set = set(occupied)
        for side, shelf_idx, cell_idx in occupied_set:
            try:
                side_enum = StorageModuleSide(side)
                location = StorageModuleCellLocation(side=side_enum, shelf_idx=shelf_idx, cell_idx=cell_idx)
                self._grid.store_item(location, None)
            except (ValueError, IndexError) as e:
                self._logger.warning(
                    "sync_occupied_cells_skip: side=%s shelf=%d cell=%d error=%s",
                    side, shelf_idx, cell_idx, str(e)
                )

    def get_param(self, param: str):
        try:
            if hasattr(self._config.robot, param):
                return getattr(self._config.robot, param)
            return self._config.robot.grid.get_param(param)
        except Exception as e:
            raise ConfigError(f"Failed to get parameter {param}: {str(e)}") from e

    async def io_place(self, coord: AxisCoordinate, nudge_override: float | None = None):
        ppl = await self._tool.get_param("PICKUP_POSITION_L")
        pph = await self._tool.get_param("PICKUP_POSITION_H")
        if ppl is None or pph is None:
            raise ConfigError("Missing PICKUP_POSITION_L or PICKUP_POSITION_H parameter")

        if self._tool._my is None:
            raise ConfigError("Y-axis motor not configured")

        await self._tool.pick(coord)
        await self._tool.rotate_to_position("front")
        await self._tool.move(AxisCoordinate(ppl[0], ppl[1]))
        await self._io.open_door()
        await self._tool.extend_IO(nudge_override=nudge_override)
        await self._tool._my.move_to_position(pph[1])
        await self._tool.retract()
        await self._io.close_door()
        await self._tool.move(coord)
        await self._tool.place(coord)

        await self._io.home_door()
        await self._tool.clear_alarms()

    async def io_pick(self, coord: AxisCoordinate):
        ppl = await self._tool.get_param("PICKUP_POSITION_L")
        pph = await self._tool.get_param("PICKUP_POSITION_H")
        if ppl is None or pph is None:
            raise ConfigError("Missing PICKUP_POSITION_L or PICKUP_POSITION_H parameter")

        if self._tool._my is None:
            raise ConfigError("Y-axis motor not configured")

        await self._tool.pick(coord)
        await self._tool.rotate_to_position("front")
        await self._tool.move(AxisCoordinate(pph[0], pph[1]))
        await self._io.open_door()
        await self._tool.extend()
        await self._tool._my.move_to_position(ppl[1])
        await self._tool.retract()
        await self._io.close_door()
        await self._tool.move(coord)
        await self._tool.place(coord)

        await self._io.home_door()
        await self._tool.clear_alarms()

    async def idle(self) -> None:
        idle_position = await self._tool.get_param("IDLE_POSITION")
        if idle_position is None:
            raise ConfigError("IDLE_POSITION not configured")
        try:
            ix, iy = idle_position
        except Exception as exc:
            raise ConfigError("IDLE_POSITION must be a 2-element iterable [x, y]") from exc
        await self._tool.move(AxisCoordinate(ix, iy))

    async def shutdown(self) -> None:
        await self.stop()
        if self._motorbus:
            await self._motorbus.close()
        if self._scalebus:
            await self._scalebus.close()

    def register_curtain_listener(self, listener) -> None:
        if self._io:
            self._io.set_curtain_listener(listener)

    def pause_for_curtain(self) -> None:
        self._curtain_safety.block()
        self._logger.info("Curtain guard engaged")

    def resume_after_curtain(self) -> None:
        self._curtain_safety.unblock()
        self._logger.info("Curtain guard cleared")

    def get_curtain_state(self) -> Optional[bool]:
        if self._io:
            return self._io.get_curtain_state()
        return None

    def get_door_state(self) -> Optional[bool]:
        if self._io:
            return self._io.get_door_state()
        return None

    async def refresh_door_state(self) -> Optional[bool]:
        if self._io:
            return await self._io.refresh_door_state()
        return None

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
                    raise RobotConfigError(f"{name} is not defined")
                if isinstance(value, list):
                    if len(value) != num_shelves:
                        raise RobotConfigError(
                            f"{name} length ({len(value)}) does not match detected shelves ({num_shelves})"
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
                        raise RobotConfigError(
                            f"CELLS_PER_SHELF length ({len(raw_value)}) does not match detected shelves ({num_shelves})"
                        )
                    left_vals = []
                    right_vals = []
                    for entry in raw_value:
                        if len(entry) != 2:
                            raise RobotConfigError("Each CELLS_PER_SHELF entry must have [left, right] counts")
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
            except RobotConfigError as exc:
                return RobotCommandError(error=str(exc))
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
                        raise RobotConfigError("CELL_WIDTHS list is shorter than total cell count")
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
                            calibration_point=CalibrationCoordinate(x=coord.x, y=coord.y, z_offset=z_offset),
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
                filename=self._grid_path,
            )
            storage_grid.save()
            self._grid = storage_grid

            return RobotCommandSuccessful()

        except Exception as e:
            return RobotCommandError(error=f"Storage calibration failed: {str(e)}")


    async def store(self, item_payload: Any) -> RobotResponse:
        """
        Store an item in the storage grid, finding the best available location based on item dimensions.

        Args:
            item_name: Name identifier for the item to store. Item dimensions are
                       obtained via `measure_item_dimensions`.

        Returns:
            RobotResponse indicating success or failure
        """

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

        item = CellItem(
            dimensions=Dimensions(
                width=width,
                height=height,
                length=length
            ),
            weight=weight,
            data={"name": resolved_item_name},
            upca=resolved_upca
        )

        if self._demo_cell_map is not None:
            demo_location = self._demo_cell_map.get(resolved_upca)
            if demo_location is None:
                return RobotCommandError(
                    command=command_name,
                    error=f"barcode {resolved_upca} not in demo mapping",
                )
            location = demo_location
        else:
            provided_location = None
            if isinstance(item_payload, dict):
                provided_location = item_payload.get("location")

            if provided_location is not None:
                location = StorageModuleCellLocation(
                    side=provided_location.get("side", "L"),
                    shelf_idx=int(provided_location.get("shelf_idx", 0)),
                    cell_idx=int(provided_location.get("cell_idx", 0)),
                )
            else:
                location = self._grid.find_best_slot_for_item(item)

        if not location:
            return RobotCommandError(command=command_name, error="No suitable storage location found")

        self._state.current_cell = location
        try:
            coord = self._grid.get_cell_coordinate(location)
            self._logger.info("Store: starting io_pick at %s", location)
            await self.io_pick(coord.to_axis_coordinate())
            self._logger.info("Store: io_pick completed")
            self._grid.store_item(location, item)
            self._logger.info("Store: command successful for %s at %s", resolved_item_name, location)
            return RobotCommandSuccessful(command=command_name, location=location, upca=resolved_upca, weight=weight)
        except (MotorError, MovementError, ToolError):
            raise
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

    def _require_scale(self):
        if self._io._scale is None:
            raise ConfigError("Scale is not enabled")
        return self._io._scale

    async def get_scale_data(self) -> Dict[str, Any]:
        return await self._require_scale().read_weight_data()

    async def get_scale_calibration_status(self) -> Dict[str, Any]:
        return await self._require_scale().get_calibration_status()

    async def get_scale_calibration_params(self) -> Dict[str, Any]:
        return await self._require_scale().get_calibration_params()

    async def set_scale_calibration_params(
        self,
        full_scale: int,
        calibration_weight: int,
        precision: int = 1,
        division: int = 1,
    ) -> bool:
        return await self._require_scale().set_calibration_params(
            full_scale=full_scale,
            calibration_weight=calibration_weight,
            precision=precision,
            division=division,
        )

    async def calibrate_scale_zero(self, timeout: float = 5.0) -> Dict[str, Any]:
        return await self._require_scale().calibrate_zero(timeout=timeout)

    async def calibrate_scale_weight(self, timeout: float = 5.0) -> Dict[str, Any]:
        return await self._require_scale().calibrate_weight(timeout=timeout)

    async def scale_tare(self) -> bool:
        return await self._require_scale().tare()

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
        door_open = await self.refresh_door_state()
        status = RobotStatus(
            gantry=gantry_status,
            tool=tool_status,
            state=state,
            storage=self._grid,
            curtain_blocked=self.get_curtain_state(),
            door_open=door_open,
        )
        self._status = status
        return status


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
                await self._tool.home()
                await self._io.home_door()
                return RobotCommandSuccessful(command=command_name)
            elif isinstance(command, MoveCommand):
                await self._tool.move(command.coordinate)
                return RobotCommandSuccessful(command=command_name)
            elif isinstance(command, IdleCommand):
                await self.idle()
                return RobotCommandSuccessful(command=command_name)
            elif isinstance(command, StoreItemCommand):
                payload = getattr(command, "upca", None)
                if payload is None:
                    payload = getattr(command, "item", None)
                return await self.store(payload)
            elif isinstance(command, RetrieveItemCommand):
                if command.upca:
                    return await self.retrieve_by_upca(command.upca, location_payload=command.location)
                if command.location:
                    return await self.retrieve_by_location(command.location)
                return RobotCommandError(command=command_name, error="Missing upca or location for retrieval")
            else:
                return RobotCommandError(command=command_name, error="Invalid Robot Command")
        except (MotorError, MovementError, ToolError):
            raise
        except Exception as exc:
            self._logger.exception("Unhandled error while processing command %s", command_name)
            sentry_sdk.capture_exception(exc)
            return RobotCommandError(
                command=command_name,
                error=f"{type(exc).__name__}: {exc}"
            )

    async def _find_location_for_upca(self, upca: str) -> Optional[StorageModuleCellLocation]:
        """Find the storage location for a given UPC in the grid."""
        for entry in self._grid.get_all_cell_data():
            cell = entry.cell
            if cell is None:
                continue
            if cell.upca == upca:
                return entry.location
            if isinstance(cell.data, dict) and cell.data.get("upca") == upca:
                return entry.location
        return None

    async def retrieve_by_upca(
        self,
        upca: str,
        *,
        location_payload: Optional[Dict[str, Any]] = None,
    ) -> RobotResponse:
        """
        Retrieve an item using its UPC, optionally with a provided location hint.
        """
        command_name = "retrieve_item"

        if not self._grid:
            return RobotCommandError(command=command_name, error="Storage grid not initialized")

        resolved_location: Optional[StorageModuleCellLocation] = None

        if location_payload is not None:
            try:
                resolved_location = StorageModuleCellLocation(**location_payload)
            except Exception:
                return RobotCommandError(command=command_name, error="Invalid location payload")

        if resolved_location is None:
            resolved_location = await self._find_location_for_upca(upca)
            if resolved_location is None:
                return RobotCommandError(command=command_name, error=f"Item '{upca}' not found in storage")

        return await self.retrieve_by_location(resolved_location.model_dump(), upca_hint=upca)

    async def retrieve_by_location(
        self,
        location_payload: Dict[str, Any],
        upca_hint: Optional[str] = None,
    ) -> RobotResponse:
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

        cached_item = cell.item
        if cached_item is None and upca_hint is None:
            self._logger.info(
                "No cached item for location %s/%s/%s; proceeding with retrieval based on command payload",
                location.side,
                location.shelf_idx,
                location.cell_idx,
            )

        self._state.current_cell = location
        retrieved_upca: str | None = upca_hint
        if cached_item is not None:
            upca_from_data = cached_item.data.get("upca") if cached_item.data else None
            retrieved_upca = cached_item.upca or (str(upca_from_data) if upca_from_data is not None else None)

        try:
            coord = self._grid.get_cell_coordinate(location).to_axis_coordinate()

            nudge_override = self._get_demo_nudge(location) if self._demo_cell_map is not None else None

            await self.io_place(coord, nudge_override=nudge_override)
            self._grid.clear_item(location)
            return RobotCommandSuccessful(command=command_name, location=location, upca=retrieved_upca)
        except (MotorError, MovementError, ToolError):
            raise
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

    def get_motor_names(self) -> Dict[str, List[str]]:
        return {
            "io": ["door"],
            "tool": ["x_axis", "y_axis", "z_axis", "stage", "rotate"],
        }

    def _get_motor_by_name(self, name: str) -> Optional["Motor"]:
        tool_motors = {
            "x_axis": self._tool._mx,
            "y_axis": self._tool._my,
            "z_axis": self._tool._mg,
            "stage": self._tool._ms,
            "rotate": self._tool._mr,
        }
        io_motors = {
            "door": self._io._md,
        }
        return tool_motors.get(name) or io_motors.get(name)

    def simulate_motor_crash(self, motor_name: str) -> bool:
        motor = self._get_motor_by_name(motor_name)

        if motor is None:
            return False

        motor.set_fault()
        return True

    async def release_gripper(self) -> None:
        await self._tool._gs.close()

    async def grip(self) -> None:
        await self._tool._gs.open()

    def reload_parameters(self) -> None:
        """Reload tool and IO parameters from config.json without restarting."""
        self._tool.reload_parameters()
        self._io.reload_parameters()
