"""Tray allocation, checkout, and independent barcode returns."""

from __future__ import annotations


import logging
import math


from datetime import datetime, timezone


from typing import Any, Callable, Optional, Protocol, Sequence, cast


from uuid import uuid4


from sqlalchemy.engine import CursorResult
from sqlalchemy import func, select, update


from sqlalchemy.ext.asyncio import AsyncSession


from openshelf_os.exceptions import (
    GRIP_DROPPED_CODE,
    GRIP_IO_BOX_FAILED_CODE,
    GRIP_PICK_FAILED_CODE,
)
from openshelf_os.robot.commands import DeliverTrayCommand, ReturnTrayCommand


from openshelf_os.robot.worker import RobotWorker


from openshelf_os_server.core.manual_motion import get_manual_motion_operation


from openshelf_os_server.core.laser_auto_calibration import is_laser_auto_calibration_running


from openshelf_os_server.database.models import (
    Item,
    ItemLocation,
    StorageBin,
    StorageBinItem,
    StorageBinWorkflow,
)


from openshelf_os_server.core.command_lifecycle import enqueue_internal


from openshelf_os_server.settings import get_root_config


ACTIVE_STATES = {"retrieving_bin", "awaiting_operator", "returning_bin"}

# An import fills one tray with several small items per trip. Its workflow names no single
# item; members join the tray as each one is placed, and the tray goes back through the
# ordinary return workflow.
BATCH_OPERATION = "batch_store"

# A tray's inner dimensions span its outer walls; the walls and every divider between
# compartments share one thickness, and each compartment keeps some leeway for placement.
TRAY_WALL_THICKNESS_MM = 5.0
COMPARTMENT_LEEWAY_MM = 2.0

# Robot failure codes that stop operation and open the crash dialog: a hardware fault raised
# mid-motion, an emergency stop interrupting the command, a tray lost between shelf and IO box,
# or a tray left in the IO box by a failed grip check. Plain command errors do not.
CRASH_FAILURE_CODES = {
    "execution_error",
    "command_interrupted",
    GRIP_DROPPED_CODE,
    GRIP_IO_BOX_FAILED_CODE,
}

# A pick whose grip check failed before anything was lifted: the tray never left its shelf, so
# cancelling the command leaves inventory as it was and no tray needs operator recovery.
TRAY_NEVER_LEFT_CODES = {GRIP_PICK_FAILED_CODE}

logger = logging.getLogger(__name__)


class RobotEventPublisher(Protocol):
    async def publish_event(self, payload: dict[str, Any]) -> None: ...


async def ensure_worker_queue_empty_for_bin_start(worker: RobotWorker) -> None:
    """Reject a new bin retrieval while any unrelated robot work is active."""
    if get_manual_motion_operation() not in {None, "robot_command_enqueue"}:
        raise BinWorkflowConflict("Robot is busy with manual motion")

    worker_state = cast(dict[str, Any], getattr(worker, "__dict__", {}))
    if worker_state.get("_demo_cycling") is True:
        raise BinWorkflowConflict("Demo cycle is already running")

    # A laser session is scheduled before its background task has acquired the
    # manual-motion gate, so inspect its durable-in-process admission marker too.
    if await is_laser_auto_calibration_running():
        raise BinWorkflowConflict("Laser auto calibration is running")

    if worker_state.get("_current_command") is not None:
        raise BinWorkflowConflict("Robot is busy processing another command")

    command_queue = worker_state.get("_command_queue")
    qsize = getattr(command_queue, "qsize", None)
    if callable(qsize) is True:
        try:
            qsize_fn = cast(Callable[[], int], qsize)
            if qsize_fn() > 0:
                raise BinWorkflowConflict("Robot has queued commands")
        except BinWorkflowConflict:
            raise
        except Exception:
            pass


async def is_io_box_occupied(worker: RobotWorker) -> bool:
    """Read IO-box occupancy using the cached scale status when available."""
    cached = worker.get_cached_scale_status()
    if cached is not None:
        return cached.occupied is True
    try:
        return abs(float((await worker.get_scale_data())["weight"])) > 5.0
    except Exception:
        return False


def ensure_worker_homed_for_bin_motion(worker: RobotWorker) -> None:
    """Reject bin motion until an explicitly requested home has completed."""
    if worker.needs_home():
        reason = worker.needs_home_reason()
        message = "Home required before continuing the storage bin workflow"
        if reason:
            message = f"{message}: {reason}"
        raise BinWorkflowConflict(message)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _history_entry(state: str, detail: Optional[str] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"state": state, "at": _utc_now().isoformat()}
    if detail:
        payload["detail"] = detail
    return payload


def _location(bin_row: StorageBin) -> dict[str, Any]:
    return {"side": bin_row.side, "shelf_idx": bin_row.shelf_idx, "cell_idx": bin_row.cell_idx}


class BinWorkflowConflict(RuntimeError):
    pass


class DirectStoreReservationError(ValueError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _location_from_payload(payload: Any) -> tuple[str, int, int] | None:
    if not isinstance(payload, dict):
        return None

    side = payload.get("side")
    shelf_idx = payload.get("shelf_idx")
    cell_idx = payload.get("cell_idx")
    if not isinstance(side, str) or shelf_idx is None or cell_idx is None:
        return None
    try:
        return side, int(shelf_idx), int(cell_idx)
    except (TypeError, ValueError):
        return None


def configured_bin_barcodes() -> set[str]:
    """Return configured bin identities, before they have database rows."""
    return {entry.barcode for entry in get_root_config().storage.bins}


def configured_bin_locations() -> set[tuple[str, int, int]]:
    """Return configured bin homes, before they have database rows."""
    return {
        (entry.home_location.side, entry.home_location.shelf_idx, entry.home_location.cell_idx)
        for entry in get_root_config().storage.bins
    }


async def reserved_bin_locations(session: AsyncSession) -> set[tuple[str, int, int]]:
    """Return homes of registered trays that automatic storage must avoid.

    A tray released by removing its configuration stops reserving its home; an explicit
    location edit may use a tray home, and the tray's return is what rejects it then.
    """
    locations = configured_bin_locations()
    rows = (
        await session.execute(select(StorageBin).where(StorageBin.active.is_(True)))
    ).scalars()
    locations.update((row.side, row.shelf_idx, row.cell_idx) for row in rows)
    return locations


async def reserved_bin_barcodes(session: AsyncSession) -> set[str]:
    """Return identities of registered trays that application-created items cannot claim."""
    rows = (
        await session.execute(select(StorageBin.barcode).where(StorageBin.active.is_(True)))
    ).scalars()
    return configured_bin_barcodes() | set(rows)


async def is_reserved_bin_barcode(barcode: str, session: AsyncSession) -> bool:
    return barcode in await reserved_bin_barcodes(session)


async def is_reserved_bin_location(location: tuple[str, int, int], session: AsyncSession) -> bool:
    return location in await reserved_bin_locations(session)


async def validate_direct_store_reservation(
    item_data: dict[str, Any], session: AsyncSession
) -> None:
    """Reject public direct-store requests that target a bin identity or member.

    A tray interaction that merely names the item does not reserve it: the workflow rejects
    conflicting state when it completes instead of locking the item meanwhile. A tray home is
    an ordinary target cell; the tray's return takes it out of service until the cell is free.
    """
    barcode = item_data.get("barcode")
    if isinstance(barcode, str) and await is_reserved_bin_barcode(barcode, session):
        raise DirectStoreReservationError(
            "Storage bin barcodes cannot be stored with a direct robot command",
            "reserved_bin_barcode",
        )

    if isinstance(barcode, str):
        item_id = await session.scalar(select(Item.id).where(Item.barcode == barcode))
        if item_id is not None:
            service = BinStorageService(session)
            if await service.item_bin(item_id) is not None:
                raise DirectStoreReservationError(
                    "Item is stored in a bin; use the storage bin workflow",
                    "reserved_tray_item",
                )


class BinStorageService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def sync_config(self) -> None:
        """Upsert configured bins and deactivate removed, unused definitions."""
        configured = get_root_config().storage.bins
        configured_barcodes = configured_bin_barcodes()
        existing = {
            row.barcode: row
            for row in (await self.session.execute(select(StorageBin))).scalars().all()
        }
        for entry in configured:
            barcode_item = (
                await self.session.execute(select(Item).where(Item.barcode == entry.barcode))
            ).scalar_one_or_none()
            if barcode_item is not None:
                raise ValueError(
                    f"Storage bin barcode {entry.barcode} is already assigned to an item"
                )
            direct_occupant = (
                await self.session.execute(
                    select(ItemLocation).where(
                        ItemLocation.side == entry.home_location.side,
                        ItemLocation.shelf_idx == entry.home_location.shelf_idx,
                        ItemLocation.cell_idx == entry.home_location.cell_idx,
                    )
                )
            ).scalar_one_or_none()
            if direct_occupant is not None:
                # A recorded item can occupy a tray home while the tray is away; it is the
                # tray's return, not configuration, that rejects that state.
                logger.warning(
                    "bin_home_occupied_by_direct_item",
                    extra={
                        "barcode": entry.barcode,
                        "side": entry.home_location.side,
                        "shelf_idx": entry.home_location.shelf_idx,
                        "cell_idx": entry.home_location.cell_idx,
                    },
                )
            row = existing.get(entry.barcode)
            inner_dimensions = entry.inner_dimensions.model_dump(mode="json")
            max_item_dimensions = getattr(entry, "max_item_dimensions", None)
            layout = getattr(entry, "compartment_layout", None)
            layout_value = layout.model_dump(mode="json") if layout is not None else None
            values = {
                "compartment_layout": layout_value,
                "side": entry.home_location.side,
                "shelf_idx": entry.home_location.shelf_idx,
                "cell_idx": entry.home_location.cell_idx,
                "inner_dimensions": inner_dimensions,
                "max_item_dimensions": (
                    max_item_dimensions.model_dump(mode="json")
                    if max_item_dimensions is not None
                    else {
                        "width": inner_dimensions["width"],
                        "length": inner_dimensions["length"],
                    }
                ),
                "max_items": entry.max_items,
                "active": True,
            }
            if row is None:
                self.session.add(StorageBin(barcode=entry.barcode, status="at_home", **values))
            else:
                geometry_changed = any(
                    getattr(row, key) != values[key]
                    for key in (
                        "compartment_layout",
                        "inner_dimensions",
                        "max_item_dimensions",
                        "max_items",
                        "side",
                        "shelf_idx",
                        "cell_idx",
                    )
                )
                if geometry_changed and (row.compartment_layout or layout_value):
                    unfinished = await self.session.scalar(
                        select(StorageBinWorkflow.id)
                        .where(
                            StorageBinWorkflow.bin_id == row.id,
                            StorageBinWorkflow.state.in_(ACTIVE_STATES),
                        )
                        .limit(1)
                    )
                    if row.status != "at_home" or await self._member_count(row.id) or unfinished:
                        raise ValueError(
                            "Empty and return the tray before changing its compartment layout, dimensions or home cell"
                        )
                for key, value in values.items():
                    setattr(row, key, value)

        for barcode, row in existing.items():
            if barcode not in configured_barcodes:
                member_count = await self._member_count(row.id)
                if member_count == 0 and row.status == "at_home":
                    row.active = False
        await self.session.commit()

    async def _member_count(self, bin_id: int) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(StorageBinItem).where(StorageBinItem.bin_id == bin_id)
        )
        return int(result.scalar_one())

    async def list_bins(self) -> list[dict[str, Any]]:
        rows = (
            (
                await self.session.execute(
                    select(StorageBin).order_by(
                        StorageBin.side, StorageBin.shelf_idx, StorageBin.cell_idx
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            await self.serialize_bin(row)
            for row in rows
            if row.active or await self._member_count(row.id) > 0
        ]

    async def _contents(
        self, bin_ids: Sequence[int]
    ) -> dict[int, list[tuple[Item, Optional[int]]]]:
        """Read the contents of several bins in one query, keyed by bin id.

        Contents and positions are read together: retrieval can remove membership concurrently.
        """
        if not bin_ids:
            return {}
        rows = (
            await self.session.execute(
                select(Item, StorageBinItem.compartment, StorageBinItem.bin_id)
                .join(StorageBinItem, StorageBinItem.item_id == Item.id)
                .where(StorageBinItem.bin_id.in_(bin_ids))
                .order_by(StorageBinItem.bin_id, Item.name, Item.barcode)
            )
        ).all()
        contents: dict[int, list[tuple[Item, Optional[int]]]] = {}
        for member, compartment, bin_id in rows:
            contents.setdefault(int(bin_id), []).append((member, compartment))
        return contents

    def _serialize_bin_row(
        self, row: StorageBin, members: Sequence[tuple[Item, Optional[int]]]
    ) -> dict[str, Any]:
        capacity_volume = self._volume(row.inner_dimensions) or 0.0
        member_volumes = [self._volume(item.dimensions) for item, _ in members]
        used_volume = (
            sum(volume for volume in member_volumes if volume is not None)
            if all(volume is not None for volume in member_volumes)
            else capacity_volume
        )
        return {
            "id": row.id,
            "barcode": row.barcode,
            "home_location": _location(row),
            "inner_dimensions": row.inner_dimensions,
            "max_item_dimensions": self._max_item_dimensions(row),
            "max_items": row.max_items,
            "compartment_layout": row.compartment_layout,
            "compartment_dimensions": self._compartment_dimensions(row)
            if row.compartment_layout
            else None,
            "item_count": len(members),
            "used_volume": used_volume,
            "available_volume": max(0.0, capacity_volume - used_volume),
            "status": row.status,
            "gross_weight": row.gross_weight if row.status == "at_home" else None,
            "active": row.active,
            "items": [
                {"id": item.id, "barcode": item.barcode, "name": item.name, "compartment": compartment}
                for item, compartment in members
            ],
        }

    async def serialize_bin(self, row: StorageBin) -> dict[str, Any]:
        members = await self._contents([int(row.id)])
        return self._serialize_bin_row(row, members.get(int(row.id), []))

    async def item_bin(self, item_id: int) -> Optional[StorageBin]:
        return (
            await self.session.execute(
                select(StorageBin)
                .join(StorageBinItem, StorageBinItem.bin_id == StorageBin.id)
                .where(StorageBinItem.item_id == item_id)
            )
        ).scalar_one_or_none()

    async def item_bin_payload(self, item_id: int) -> Optional[dict[str, Any]]:
        row = await self.item_bin(item_id)
        return await self.serialize_bin(row) if row else None

    async def item_bin_payloads(self, item_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        """Resolve bin payloads for many items at once, keyed by item id.

        A paged item list must not pay two queries per row, and items that share a tray must not
        re-read that tray once per member. Memberships, tray rows, and every tray's contents are
        read in a fixed set of queries regardless of page size. Items without a bin are omitted.
        """
        ids = list(dict.fromkeys(int(item_id) for item_id in item_ids))
        if not ids:
            return {}
        memberships = (
            await self.session.execute(
                select(StorageBinItem.item_id, StorageBinItem.bin_id).where(
                    StorageBinItem.item_id.in_(ids)
                )
            )
        ).all()
        if not memberships:
            return {}
        bin_ids = list(dict.fromkeys(int(bin_id) for _, bin_id in memberships))
        bins = (
            await self.session.execute(select(StorageBin).where(StorageBin.id.in_(bin_ids)))
        ).scalars().all()
        contents = await self._contents(bin_ids)
        payloads = {
            int(row.id): self._serialize_bin_row(row, contents.get(int(row.id), []))
            for row in bins
        }
        # An item has at most one bin (uq_storage_bin_items_item), so this mapping is unambiguous.
        return {
            int(item_id): payloads[int(bin_id)]
            for item_id, bin_id in memberships
            if int(bin_id) in payloads
        }

    async def targets_reserved_bin_identity(self, payload: dict[str, Any]) -> bool:
        """Whether legacy direct mutation would claim a reserved bin barcode or home."""
        barcode = payload.get("barcode")
        if isinstance(barcode, str) and await is_reserved_bin_barcode(barcode, self.session):
            return True

        location = _location_from_payload(payload.get("location"))
        return location is not None and await is_reserved_bin_location(location, self.session)

    async def item_has_active_workflow(self, item_id: int) -> bool:
        """Whether an unfinished take, place, or return interaction names this item."""
        workflows = (
            await self.session.execute(
                select(StorageBinWorkflow).where(StorageBinWorkflow.state.in_(ACTIVE_STATES))
            )
        ).scalars()
        return any(
            workflow.item_id == item_id
            or (
                workflow.operation == "return"
                and item_id in set((workflow.resolution or {}).get("candidate_item_ids", []))
            )
            for workflow in workflows
        )

    async def item_has_workflow_history(self, item_id: int) -> bool:
        """Whether an item is referenced by a durable bin workflow record."""
        workflow = (
            await self.session.execute(
                select(StorageBinWorkflow.id).where(StorageBinWorkflow.item_id == item_id).limit(1)
            )
        ).scalar_one_or_none()
        return workflow is not None

    @staticmethod
    def _dimensions(
        value: dict[str, Any] | None, keys: tuple[str, ...]
    ) -> Optional[tuple[float, ...]]:
        try:
            dimensions = tuple(float((value or {})[key]) for key in keys)
        except (KeyError, TypeError, ValueError):
            return None
        return dimensions if all(dimension > 0 for dimension in dimensions) else None

    @classmethod
    def _volume(cls, value: dict[str, Any] | None) -> Optional[float]:
        dimensions = cls._dimensions(value, ("width", "height", "length"))
        if dimensions is None:
            return None
        width, height, length = dimensions
        return width * height * length

    @staticmethod
    def _compartment_span(inside: float, count: int) -> float:
        walls = (count + 1) * TRAY_WALL_THICKNESS_MM
        return max(0.0, (inside - walls) / count - COMPARTMENT_LEEWAY_MM)

    @classmethod
    def _compartment_dimensions(cls, bin_row: StorageBin) -> dict[str, float]:
        inside = bin_row.inner_dimensions
        layout = bin_row.compartment_layout
        return {
            "width": cls._compartment_span(inside["width"], layout["columns"] if layout else 1),
            "height": inside["height"],
            "length": cls._compartment_span(inside["length"], layout["rows"] if layout else 1),
        }

    async def _free_compartment(self, bin_row: StorageBin) -> Optional[int]:
        layout = bin_row.compartment_layout
        if not layout:
            return None
        occupied = list(
            (
                await self.session.execute(
                    select(StorageBinItem.compartment).where(StorageBinItem.bin_id == bin_row.id)
                )
            ).scalars()
        )
        count = layout["rows"] * layout["columns"]
        # Unknown physical positions cannot safely be treated as empty slots.
        if any(not isinstance(number, int) or number < 1 or number > count for number in occupied):
            return None
        return next((number for number in range(1, count + 1) if number not in occupied), None)

    async def _compartment_error(
        self,
        workflow: StorageBinWorkflow,
        bin_row: StorageBin,
        confirmed_ids: Optional[Sequence[int]] = None,
    ) -> Optional[str]:
        layout = bin_row.compartment_layout
        if not layout:
            return None
        if workflow.operation == "return":
            positions = (workflow.resolution or {}).get("candidate_compartments", {})
            selected = [
                positions.get(str(item_id))
                for item_id in (
                    confirmed_ids
                    if confirmed_ids is not None
                    else (workflow.resolution or {}).get("confirmed_item_ids", [])
                )
            ]
        else:
            membership = await self.session.scalar(
                select(StorageBinItem).where(StorageBinItem.item_id == workflow.item_id)
            )
            if membership is not None and (
                membership.bin_id != bin_row.id or membership.compartment != workflow.compartment
            ):
                return "Item compartment no longer matches the workflow"
            selected = [workflow.compartment]
            others = (
                (
                    await self.session.execute(
                        select(StorageBinItem.compartment).where(
                            StorageBinItem.bin_id == bin_row.id,
                            StorageBinItem.item_id != workflow.item_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            selected.extend(others)
        count = layout["rows"] * layout["columns"]
        if any(
            not isinstance(number, int) or isinstance(number, bool) or number < 1 or number > count
            for number in selected
        ):
            return "A tray item has no valid compartment assignment"
        if len(selected) != len(set(selected)):
            return "Two tray items cannot occupy the same compartment"
        return None

    @classmethod
    def _max_item_dimensions(cls, bin_row: StorageBin) -> dict[str, float]:
        configured = cls._dimensions(bin_row.max_item_dimensions, ("width", "length"))
        if configured is not None:
            slot = cls._compartment_dimensions(bin_row)
            return {
                "width": min(configured[0], slot["width"]),
                "length": min(configured[1], slot["length"]),
            }
        inside = cls._dimensions(cls._compartment_dimensions(bin_row), ("width", "length"))
        if inside is None:
            return {"width": 0.0, "length": 0.0}
        return {"width": inside[0], "length": inside[1]}

    @classmethod
    def _fits(cls, item: Item, bin_row: StorageBin) -> bool:
        dimensions = cls._dimensions(item.dimensions, ("width", "height", "length"))
        inside = cls._dimensions(
            cls._compartment_dimensions(bin_row), ("width", "height", "length")
        )
        maximum = cls._dimensions(cls._max_item_dimensions(bin_row), ("width", "length"))
        if dimensions is None or inside is None or maximum is None:
            return False
        width, height, length = dimensions
        inner_width, inner_height, inner_length = inside
        max_width, max_length = maximum
        return height <= inner_height and (
            (
                width <= inner_width
                and length <= inner_length
                and width <= max_width
                and length <= max_length
            )
            or (
                length <= inner_width
                and width <= inner_length
                and length <= max_width
                and width <= max_length
            )
        )

    @classmethod
    def _has_volume_for(cls, item: Item, members: Sequence[Item], bin_row: StorageBin) -> bool:
        capacity = cls._volume(bin_row.inner_dimensions)
        item_volume = cls._volume(item.dimensions)
        member_volumes = [cls._volume(member.dimensions) for member in members]
        if (
            capacity is None
            or item_volume is None
            or any(volume is None for volume in member_volumes)
        ):
            return False
        return (
            sum(volume for volume in member_volumes if volume is not None) + item_volume <= capacity
        )

    @classmethod
    def _contents_have_volume(cls, items: Sequence[Item], bin_row: StorageBin) -> bool:
        capacity = cls._volume(bin_row.inner_dimensions)
        item_volumes = [cls._volume(item.dimensions) for item in items]
        if capacity is None or any(volume is None for volume in item_volumes):
            return False
        return sum(volume for volume in item_volumes if volume is not None) <= capacity

    async def allocate_bin(self, item: Item) -> Optional[StorageBin]:
        bins = (
            (
                await self.session.execute(
                    select(StorageBin).where(
                        StorageBin.active.is_(True), StorageBin.status == "at_home"
                    )
                )
            )
            .scalars()
            .all()
        )
        candidates: list[tuple[bool, float, str, int, int, StorageBin]] = []
        for row in bins:
            members = (
                (
                    await self.session.execute(
                        select(Item)
                        .join(StorageBinItem, StorageBinItem.item_id == Item.id)
                        .where(StorageBinItem.bin_id == row.id)
                    )
                )
                .scalars()
                .all()
            )
            if (
                len(members) >= row.max_items
                or (
                    row.compartment_layout is not None and await self._free_compartment(row) is None
                )
                or not self._fits(item, row)
                or not self._has_volume_for(item, members, row)
            ):
                continue
            if any(member.access_level_id != item.access_level_id for member in members):
                continue
            capacity = self._volume(row.inner_dimensions)
            item_volume = self._volume(item.dimensions)
            used_volume = sum(self._volume(member.dimensions) or 0.0 for member in members)
            if capacity is None or item_volume is None:
                continue
            remaining_volume = (
                (self._volume(self._compartment_dimensions(row)) or capacity) - item_volume
                if row.compartment_layout
                else capacity - used_volume - item_volume
            )
            candidates.append(
                (
                    len(members) == 0,
                    remaining_volume,
                    row.side,
                    row.shelf_idx,
                    row.cell_idx,
                    row,
                )
            )
        if not candidates:
            return None
        return min(candidates, key=lambda entry: entry[:-1])[-1]

    async def _apply_return_contents(
        self, workflow: StorageBinWorkflow, bin_row: StorageBin
    ) -> None:
        confirmed_ids = set((workflow.resolution or {}).get("confirmed_item_ids", []))
        memberships = (
            (
                await self.session.execute(
                    select(StorageBinItem).where(StorageBinItem.bin_id == bin_row.id)
                )
            )
            .scalars()
            .all()
        )
        current_ids = {membership.item_id for membership in memberships}
        for membership in memberships:
            if membership.item_id not in confirmed_ids:
                await self.session.delete(membership)
        await self.session.flush()
        positions = (workflow.resolution or {}).get("candidate_compartments", {})
        for item_id in confirmed_ids - current_ids:
            self.session.add(
                StorageBinItem(
                    bin_id=bin_row.id, item_id=item_id, compartment=positions.get(str(item_id))
                )
            )
        if confirmed_ids:
            await self.session.execute(
                update(Item)
                .where(Item.id.in_(confirmed_ids))
                .values(enrollment_weight_pending=False, enrollment_storage_completed=True)
            )

    async def _return_contents_error(
        self, workflow: StorageBinWorkflow, bin_row: StorageBin
    ) -> Optional[str]:
        occupied_home = (
            await self.session.execute(
                select(ItemLocation.id)
                .where(
                    ItemLocation.side == bin_row.side,
                    ItemLocation.shelf_idx == bin_row.shelf_idx,
                    ItemLocation.cell_idx == bin_row.cell_idx,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if occupied_home is not None:
            return "Tray home cell is occupied by an item"
        compartment_error = await self._compartment_error(workflow, bin_row)
        if compartment_error:
            return compartment_error
        candidate_ids = set((workflow.resolution or {}).get("candidate_item_ids", []))
        confirmed_ids = set((workflow.resolution or {}).get("confirmed_item_ids", []))
        if not confirmed_ids.issubset(candidate_ids):
            return "Confirmed bin contents include an unexpected item"
        items = (
            (await self.session.execute(select(Item).where(Item.id.in_(confirmed_ids))))
            .scalars()
            .all()
            if confirmed_ids
            else []
        )
        if len(items) != len(confirmed_ids):
            return "A confirmed bin item no longer exists"
        if len(items) > bin_row.max_items:
            return "Storage bin has reached its item limit"
        # A current member is already inside the tray; re-checking its fit would strand the
        # tray outside once compartment sizing tightens after the item was stored.
        members = set(
            (
                await self.session.scalars(
                    select(StorageBinItem.item_id).where(StorageBinItem.bin_id == bin_row.id)
                )
            ).all()
        )
        if any(item.id not in members and not self._fits(item, bin_row) for item in items):
            return "A confirmed item no longer fits the configured storage bin"
        if not self._contents_have_volume(items, bin_row):
            return "Confirmed bin contents exceed the configured storage capacity"
        if len({item.access_level_id for item in items}) > 1:
            return "Confirmed items do not share the same access level"
        direct_item = (
            await self.session.execute(
                select(ItemLocation.item_id).where(ItemLocation.item_id.in_(confirmed_ids)).limit(1)
            )
        ).scalar_one_or_none()
        if direct_item is not None:
            return "A confirmed item has a direct storage location"
        other_membership = (
            await self.session.execute(
                select(StorageBinItem.item_id)
                .where(
                    StorageBinItem.item_id.in_(confirmed_ids),
                    StorageBinItem.bin_id != bin_row.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if other_membership is not None:
            return "A confirmed item is associated with another storage bin"
        if bin_row.compartment_layout:
            positions = (workflow.resolution or {}).get("candidate_compartments", {})
            memberships = await self.session.scalars(
                select(StorageBinItem).where(
                    StorageBinItem.bin_id == bin_row.id,
                    StorageBinItem.item_id.in_(confirmed_ids),
                )
            )
            if any(member.compartment != positions.get(str(member.item_id)) for member in memberships):
                return "Item compartment no longer matches the workflow"
        return None

    async def _store_invariant_error(self, bin_row: StorageBin, item: Item) -> Optional[str]:
        """Validate durable bin/item invariants without requiring live motion state."""
        if not bin_row.active or not self._fits(item, bin_row):
            return "Item no longer fits the configured storage bin"

        direct_location = (
            await self.session.execute(
                select(ItemLocation.id).where(ItemLocation.item_id == item.id).limit(1)
            )
        ).scalar_one_or_none()
        if direct_location is not None:
            return "Item has a direct storage location"

        members = (
            (
                await self.session.execute(
                    select(Item)
                    .join(StorageBinItem, StorageBinItem.item_id == Item.id)
                    .where(StorageBinItem.bin_id == bin_row.id)
                )
            )
            .scalars()
            .all()
        )
        if len(members) >= bin_row.max_items:
            return "Storage bin has reached its item limit"
        if not self._has_volume_for(item, members, bin_row):
            return "Storage bin does not have enough available space"
        if any(member.access_level_id != item.access_level_id for member in members):
            return "Item access level no longer matches the storage bin"
        return None

    async def active_workflows(self) -> list[StorageBinWorkflow]:
        return list(
            (
                await self.session.scalars(
                    select(StorageBinWorkflow)
                    .where(StorageBinWorkflow.state.in_(ACTIVE_STATES))
                    .order_by(StorageBinWorkflow.created_at)
                )
            ).all()
        )

    async def recover_interrupted_workflows(self) -> None:
        # Never replay physical motion, or make an interrupted tray available.
        for workflow in await self.active_workflows():
            if (workflow.resolution or {}).get("physical_return_completed"):
                await self.complete_return(workflow)
            elif workflow.state in {"retrieving_bin", "returning_bin"}:
                await self.fail(workflow, "Robot process restarted during tray motion")

    async def serialize_workflow(self, row: StorageBinWorkflow) -> dict[str, Any]:
        tray = await self.session.get(StorageBin, row.bin_id)
        item = await self.session.get(Item, row.item_id) if row.item_id else None
        if tray is None:
            raise BinWorkflowConflict("Tray no longer exists")
        result = {
            "id": row.id,
            "operation": row.operation,
            "state": row.state,
            "compartment": row.compartment,
            "bin": await self.serialize_bin(tray),
            "item": {"id": item.id, "barcode": item.barcode, "name": item.name} if item else None,
            "error": row.error,
            "history": row.history or [],
            "resolution": row.resolution,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }
        if row.operation == "return":
            positions = (row.resolution or {}).get("candidate_compartments", {})
            candidates = (row.resolution or {}).get("candidate_item_ids", [])
            confirmed_ids = set((row.resolution or {}).get("confirmed_item_ids", []))
            items = (
                await self.session.scalars(
                    select(Item).where(Item.id.in_(candidates)).order_by(Item.name)
                )
            ).all()
            result["contents"] = [
                dict(
                    id=i.id,
                    barcode=i.barcode,
                    name=i.name,
                    confirmed=i.id in confirmed_ids,
                    compartment=positions.get(str(i.id)),
                )
                for i in items
            ]
        return result

    async def transition(
        self, workflow: StorageBinWorkflow, state: str, detail: Optional[str] = None
    ) -> None:
        workflow.state = state
        workflow.updated_at = _utc_now()
        workflow.history = [*(workflow.history or []), _history_entry(state, detail)]
        if state in {"completed", "cancelled", "failed"}:
            workflow.completed_at = _utc_now()
        await self.session.commit()

    async def fail(
        self,
        workflow: StorageBinWorkflow,
        error: str,
        *,
        code: Optional[str] = None,
        tray_moved: bool = True,
        state: str = "failed",
    ) -> None:
        workflow.error = error
        if code is not None:
            workflow.resolution = {**(workflow.resolution or {}), "failure_code": code}
        tray = await self.session.get(StorageBin, workflow.bin_id)
        if tray is not None:
            # Releasing the claim of a motion that never started leaves the tray on its shelf.
            tray.status = "outside" if tray_moved else "at_home"
            tray.gross_weight = None
        await self.transition(workflow, state, error)

    async def crash_interrupted_workflows(self) -> list[StorageBinWorkflow]:
        """Failed tray motions a crash or emergency stop interrupted, while their tray is still outside.

        Only the latest workflow of each tray counts: a later interaction supersedes the failure.
        """
        trays = (
            await self.session.scalars(select(StorageBin).where(StorageBin.status == "outside"))
        ).all()
        interrupted: list[StorageBinWorkflow] = []
        for tray in trays:
            latest = await self.session.scalar(
                select(StorageBinWorkflow)
                .where(StorageBinWorkflow.bin_id == tray.id)
                .order_by(StorageBinWorkflow.created_at.desc())
                .limit(1)
            )
            if (
                latest is not None
                and latest.state == "failed"
                and (latest.resolution or {}).get("failure_code") in CRASH_FAILURE_CODES
            ):
                interrupted.append(latest)
        return interrupted

    async def recover_crash_interrupted(self) -> list[tuple[StorageBinWorkflow, Optional[str]]]:
        """Put trays back home once the operator confirmed them on their shelves after a crash.

        An interrupted return also records the contents the operator confirmed before it
        started. Returns each interrupted workflow with the reason its tray stays outside, or
        None when the tray is home again.
        """
        results: list[tuple[StorageBinWorkflow, Optional[str]]] = []
        for workflow in await self.crash_interrupted_workflows():
            tray = await self.session.get(StorageBin, workflow.bin_id)
            assert tray is not None
            if workflow.operation == "return":
                error = await self._return_contents_error(workflow, tray)
                if error:
                    results.append((workflow, error))
                    continue
                await self._apply_return_contents(workflow, tray)
            tray.status = "at_home"
            tray.gross_weight = None
            workflow.resolution = {**(workflow.resolution or {}), "crash_recovered": True}
            workflow.history = [
                *(workflow.history or []),
                _history_entry("failed", "Operator confirmed the tray on its shelf after the crash"),
            ]
            workflow.updated_at = _utc_now()
            await self.session.commit()
            results.append((workflow, None))
        return results

    async def begin(
        self, operation: str, item: Item, user_id: Optional[int]
    ) -> Optional[StorageBinWorkflow]:
        if operation == "retrieve":
            tray = await self.item_bin(item.id)
            if tray is None:
                return None
        elif operation == "store":
            if await self.item_bin(item.id):
                raise BinWorkflowConflict("Item is already in a tray")
            tray = await self.allocate_bin(item)
            if tray is None:
                return None
        else:
            raise ValueError("Expected store or retrieve")
        # Atomic per-tray reservation, not a global operation lock.
        claimed = await self.session.execute(
            update(StorageBin)
            .where(StorageBin.id == tray.id, StorageBin.status == "at_home")
            .values(status="outside", gross_weight=None)
        )
        if cast(CursorResult, claimed).rowcount != 1:
            await self.session.rollback()
            raise BinWorkflowConflict("This tray is currently outside the system")
        if await self.item_has_active_workflow(item.id):
            # One interaction per item, not a lock on the item: an open interaction stops a
            # second one from starting, while edits and commands stay available.
            await self.session.rollback()
            raise BinWorkflowConflict("An interaction for this item is already in progress")
        compartment = (
            (
                await self.session.scalar(
                    select(StorageBinItem.compartment).where(StorageBinItem.item_id == item.id)
                )
            )
            if operation == "retrieve"
            else await self._free_compartment(tray)
        )
        members = (
            await self.session.execute(
                select(StorageBinItem.item_id, StorageBinItem.compartment).where(
                    StorageBinItem.bin_id == tray.id
                )
            )
        ).all()
        positions = {str(i): c for i, c in members}
        positions[str(item.id)] = compartment
        workflow = StorageBinWorkflow(
            id=str(uuid4()),
            operation=operation,
            state="retrieving_bin",
            bin_id=tray.id,
            item_id=item.id,
            user_id=user_id,
            compartment=compartment,
            history=[_history_entry("retrieving_bin")],
            resolution={
                "candidate_compartments": positions,
            },
        )
        error = await self._compartment_error(workflow, tray)
        if error:
            await self.session.rollback()
            raise BinWorkflowConflict(error)
        self.session.add(workflow)
        await self.session.commit()
        return workflow

    async def close(self, workflow: StorageBinWorkflow) -> None:
        if workflow.state in {"completed", "cancelled", "failed"}:
            return
        # Claim the persisted instruction before changing its state or membership.
        # The write lock lasts until transition commits, so a concurrent Store or
        # Close cannot race a cached awaiting_operator state.
        claimed = await self.session.execute(
            update(StorageBinWorkflow)
            .where(
                StorageBinWorkflow.id == workflow.id,
                StorageBinWorkflow.state == "awaiting_operator",
            )
            .values(state=StorageBinWorkflow.state)
        )
        if cast(CursorResult, claimed).rowcount != 1:
            await self.session.rollback()
            await self.session.refresh(workflow)
            if workflow.state in {"completed", "cancelled", "failed"}:
                return
            raise BinWorkflowConflict("Wait for tray motion to finish")
        await self.session.refresh(workflow)
        if workflow.operation == BATCH_OPERATION:
            await self.session.rollback()
            raise BinWorkflowConflict("Finish this tray on the import screen")
        if workflow.operation == "return":
            await self.transition(workflow, "cancelled")
            return
        tray = await self.session.get(StorageBin, workflow.bin_id)
        item = await self.session.get(Item, workflow.item_id)
        if tray is None or item is None:
            raise BinWorkflowConflict("Tray or item no longer exists")
        member = await self.session.scalar(
            select(StorageBinItem).where(StorageBinItem.item_id == item.id)
        )
        if workflow.operation == "store":
            error = await self._compartment_error(workflow, tray)
            if member is None:
                error = error or await self._store_invariant_error(tray, item)
            elif member.bin_id != tray.id:
                error = "Item is already in another tray"
            if error:
                raise BinWorkflowConflict(error)
            if member is None:
                self.session.add(
                    StorageBinItem(
                        bin_id=tray.id, item_id=item.id, compartment=workflow.compartment
                    )
                )
            item.enrollment_weight_pending = False
            item.enrollment_storage_completed = True
        elif member is not None:
            if member.bin_id != tray.id:
                raise BinWorkflowConflict("Item is already in another tray")
            await self.session.delete(member)
        await self.transition(workflow, "completed")

    async def begin_return(
        self, barcode: str, user_id: Optional[int] = None
    ) -> Optional[StorageBinWorkflow]:
        tray = await self.session.scalar(
            select(StorageBin).where(StorageBin.barcode == barcode, StorageBin.active.is_(True))
        )
        if tray is None:
            return None
        # Serialize duplicate scans on this tray across database sessions.
        await self.session.execute(
            update(StorageBin).where(StorageBin.id == tray.id).values(updated_at=_utc_now())
        )
        await self.session.refresh(tray)
        if tray.status != "outside":
            await self.session.rollback()
            raise BinWorkflowConflict("This tray is already in the system")
        pending = (
            await self.session.scalars(
                select(StorageBinWorkflow).where(
                    StorageBinWorkflow.bin_id == tray.id,
                    StorageBinWorkflow.state.in_(ACTIVE_STATES),
                )
            )
        ).all()
        for workflow in pending:
            if workflow.operation == "return":
                await self.session.commit()
                return workflow
            if workflow.operation == BATCH_OPERATION:
                # A stray tray scan must not drop the import's placements; the import
                # screen returns this tray once the operator has filled it.
                await self.session.rollback()
                raise BinWorkflowConflict("Finish this tray on the import screen")
            if workflow.state == "retrieving_bin":
                await self.session.rollback()
                raise BinWorkflowConflict("Tray delivery is still running")
            # A barcode return supersedes a dismissed/abandoned item instruction.
            workflow.state = "cancelled"
            workflow.completed_at = _utc_now()
        latest = await self.session.scalar(
            select(StorageBinWorkflow)
            .where(
                StorageBinWorkflow.bin_id == tray.id,
                StorageBinWorkflow.operation.in_(["store", "retrieve"]),
            )
            .order_by(StorageBinWorkflow.created_at.desc())
            .limit(1)
        )
        positions = (
            dict((latest.resolution or {}).get("candidate_compartments", {})) if latest else {}
        )
        if latest and latest.item_id and not positions:
            positions[str(latest.item_id)] = latest.compartment
        for member in await self.session.scalars(
            select(StorageBinItem).where(StorageBinItem.bin_id == tray.id)
        ):
            positions[str(member.item_id)] = member.compartment
        workflow = StorageBinWorkflow(
            id=str(uuid4()),
            operation="return",
            state="awaiting_operator",
            bin_id=tray.id,
            item_id=None,
            user_id=user_id,
            history=[_history_entry("awaiting_operator")],
            resolution={
                "candidate_item_ids": [int(i) for i in positions],
                "candidate_compartments": positions,
                "confirmed_item_ids": [int(i) for i in positions],
            },
        )
        self.session.add(workflow)
        await self.session.commit()
        return workflow

    async def prepare_return(self, workflow: StorageBinWorkflow, confirmed_ids: list[int]) -> bool:
        if workflow.operation != "return":
            raise BinWorkflowConflict("Scan the tray barcode to return it")
        if workflow.state in {"returning_bin", "completed"}:
            if set(confirmed_ids) != set((workflow.resolution or {}).get("confirmed_item_ids", [])):
                raise BinWorkflowConflict("Return contents were already submitted")
            return False
        if workflow.state != "awaiting_operator":
            raise BinWorkflowConflict("Scan the tray barcode to start a new return")
        tray = await self.session.get(StorageBin, workflow.bin_id)
        if tray is None:
            raise BinWorkflowConflict("Tray no longer exists")
        workflow.resolution = {**(workflow.resolution or {}), "confirmed_item_ids": confirmed_ids}
        error = await self._return_contents_error(workflow, tray)
        if error:
            await self.session.rollback()
            raise BinWorkflowConflict(error)
        claimed = await self.session.execute(
            update(StorageBinWorkflow)
            .where(
                StorageBinWorkflow.id == workflow.id,
                StorageBinWorkflow.state == "awaiting_operator",
            )
            .values(state="returning_bin")
        )
        if cast(CursorResult, claimed).rowcount != 1:
            await self.session.rollback()
            await self.session.refresh(workflow)
            return False
        await self.transition(workflow, "returning_bin")
        return True

    async def complete_return(self, workflow: StorageBinWorkflow, gross_weight: Any = None) -> None:
        if workflow.state == "completed":
            return
        tray = await self.session.get(StorageBin, workflow.bin_id)
        if tray is None:
            raise BinWorkflowConflict("Tray no longer exists")
        # Persist physical success before inventory finalization. Startup can finish
        # the database update without ever replaying the robot command.
        workflow.resolution = {**(workflow.resolution or {}), "physical_return_completed": True}
        await self.session.commit()
        # Migrated physical successes may have failed legacy inventory checks.
        # Keep the return reserved if those invariants still cannot be satisfied;
        # failing it would allow a barcode scan to admit a second physical return.
        error = await self._return_contents_error(workflow, tray)
        if error:
            workflow.error = error
            await self.session.commit()
            return
        await self._apply_return_contents(workflow, tray)
        workflow.error = None
        tray.status = "at_home"
        tray.gross_weight = None
        try:
            weight = float(gross_weight)
            if not isinstance(gross_weight, bool) and math.isfinite(weight) and weight >= 0:
                tray.gross_weight = weight
        except (TypeError, ValueError):
            pass
        await self.transition(workflow, "completed")

    async def allocate_batch_tray(
        self, access_level_id: Optional[int], wanted: int
    ) -> Optional[StorageBin]:
        """Choose a home tray that can take the most of `wanted` items of one access level.

        A tray qualifies when it has a free compartment (or room under `max_items` when it
        is undivided) and every current member shares the access level. Among trays that
        take equally many items, a partially filled one is preferred to keep trays full.
        """
        bins = (
            await self.session.scalars(
                select(StorageBin).where(
                    StorageBin.active.is_(True), StorageBin.status == "at_home"
                )
            )
        ).all()
        candidates: list[tuple[int, bool, str, int, int, StorageBin]] = []
        for row in bins:
            members = (
                await self.session.scalars(
                    select(Item)
                    .join(StorageBinItem, StorageBinItem.item_id == Item.id)
                    .where(StorageBinItem.bin_id == row.id)
                )
            ).all()
            if any(member.access_level_id != access_level_id for member in members):
                continue
            capacity = self._batch_capacity(row, len(members))
            if row.compartment_layout is not None and await self._free_compartment(row) is None:
                continue
            if capacity <= 0:
                continue
            candidates.append(
                (
                    -min(capacity, max(1, wanted)),
                    len(members) == 0,
                    row.side,
                    row.shelf_idx,
                    row.cell_idx,
                    row,
                )
            )
        if not candidates:
            return None
        return min(candidates, key=lambda entry: entry[:-1])[-1]

    @staticmethod
    def _batch_capacity(row: StorageBin, member_count: int) -> int:
        layout = row.compartment_layout
        slots = layout["rows"] * layout["columns"] if layout else row.max_items
        return max(0, min(slots, row.max_items) - member_count)

    def batch_item_dimensions(self, row: StorageBin, height: float) -> dict[str, float]:
        """Small import items take the tray's largest allowed footprint and a measured height."""
        footprint = self._max_item_dimensions(row)
        return {"width": footprint["width"], "length": footprint["length"], "height": height}

    def batch_max_height(self, row: StorageBin) -> float:
        return float(self._compartment_dimensions(row)["height"])

    async def begin_batch(
        self, tray: StorageBin, user_id: Optional[int], import_job_id: str
    ) -> StorageBinWorkflow:
        claimed = await self.session.execute(
            update(StorageBin)
            .where(StorageBin.id == tray.id, StorageBin.status == "at_home")
            .values(status="outside", gross_weight=None)
        )
        if cast(CursorResult, claimed).rowcount != 1:
            await self.session.rollback()
            raise BinWorkflowConflict("This tray is currently outside the system")
        members = (
            await self.session.execute(
                select(StorageBinItem.item_id, StorageBinItem.compartment).where(
                    StorageBinItem.bin_id == tray.id
                )
            )
        ).all()
        workflow = StorageBinWorkflow(
            id=str(uuid4()),
            operation=BATCH_OPERATION,
            state="retrieving_bin",
            bin_id=tray.id,
            item_id=None,
            user_id=user_id,
            compartment=None,
            history=[_history_entry("retrieving_bin")],
            resolution={
                "import_job_id": import_job_id,
                "candidate_compartments": {str(i): c for i, c in members},
                "placed_item_ids": [],
            },
        )
        self.session.add(workflow)
        await self.session.commit()
        return workflow

    async def batch_place_error(self, workflow: StorageBinWorkflow, item: Item) -> Optional[str]:
        """Why `item` cannot join the batch tray now, or None when it can."""
        if workflow.operation != BATCH_OPERATION:
            return "Not an import tray"
        if workflow.state != "awaiting_operator":
            return "Wait for the tray to arrive"
        tray = await self.session.get(StorageBin, workflow.bin_id)
        if tray is None:
            return "Tray no longer exists"
        if tray.compartment_layout is not None and await self._free_compartment(tray) is None:
            return "The tray is full"
        return await self._store_invariant_error(tray, item)

    async def place_in_batch(self, workflow: StorageBinWorkflow, item: Item) -> Optional[int]:
        """Record `item` in the batch tray's next free compartment and return that number.

        Membership is written when the operator places the item, as `close` does for a single
        item, so the tray return that follows confirms it with the rest of the contents.
        """
        error = await self.batch_place_error(workflow, item)
        if error:
            raise BinWorkflowConflict(error)
        tray = await self.session.get(StorageBin, workflow.bin_id)
        assert tray is not None
        compartment = await self._free_compartment(tray)
        self.session.add(StorageBinItem(bin_id=tray.id, item_id=item.id, compartment=compartment))
        item.enrollment_weight_pending = False
        item.enrollment_storage_completed = True
        resolution = dict(workflow.resolution or {})
        positions = dict(resolution.get("candidate_compartments", {}))
        positions[str(item.id)] = compartment
        resolution["candidate_compartments"] = positions
        resolution["placed_item_ids"] = [*resolution.get("placed_item_ids", []), item.id]
        workflow.resolution = resolution
        workflow.updated_at = _utc_now()
        await self.session.commit()
        return compartment

    async def close_batch(self, workflow: StorageBinWorkflow) -> None:
        """End the filling of a batch tray; its return is a separate workflow."""
        if workflow.state in {"completed", "cancelled", "failed"}:
            return
        if workflow.state != "awaiting_operator":
            raise BinWorkflowConflict("Wait for tray motion to finish")
        await self.transition(workflow, "completed", "import tray filled")

    async def start_retrieval(self, workflow: StorageBinWorkflow, worker: RobotWorker) -> None:
        tray = await self.session.get(StorageBin, workflow.bin_id)
        assert tray is not None
        await enqueue_internal(
            worker,
            DeliverTrayCommand(request_id=workflow.id, barcode=tray.barcode, location=_location(tray)),
        )

    async def enqueue_return(self, workflow: StorageBinWorkflow, worker: RobotWorker) -> None:
        tray = await self.session.get(StorageBin, workflow.bin_id)
        assert tray is not None
        await enqueue_internal(
            worker,
            ReturnTrayCommand(
                request_id=workflow.id,
                item={
                    "barcode": tray.barcode,
                    "name": f"Tray {tray.barcode}",
                    "dimensions": tray.inner_dimensions,
                    "location": _location(tray),
                },
            ),
        )

    async def publish_workflow(
        self, worker: RobotEventPublisher, workflow: StorageBinWorkflow
    ) -> None:
        await worker.publish_event(
            {"kind": "bin_workflow_updated", "payload": await self.serialize_workflow(workflow)}
        )

    async def owns_robot_event(self, payload: dict[str, Any]) -> bool:
        if payload.get("request_id") and await self.session.get(
            StorageBinWorkflow, payload["request_id"]
        ):
            return True
        return await self.targets_reserved_bin_identity(payload)

    async def handle_robot_result(
        self, payload: dict[str, Any], worker: RobotEventPublisher
    ) -> bool:
        request_id = payload.get("request_id")
        workflow = await self.session.get(StorageBinWorkflow, request_id) if request_id else None
        if workflow is None or workflow.state not in {"retrieving_bin", "returning_bin"}:
            return False
        expected = "retrieve_item" if workflow.state == "retrieving_bin" else "store_item"
        if payload.get("command") != expected:
            return False
        if payload.get("message") != "success":
            code = payload.get("code")
            error = str(payload.get("error") or "Tray motion failed")
            if code in TRAY_NEVER_LEFT_CODES:
                # Nothing was lifted, so the command is cancelled: the tray stays home and the
                # workflow leaves no unfinished motion behind.
                await self.fail(workflow, error, code=code, tray_moved=False, state="cancelled")
            else:
                await self.fail(workflow, error, code=code)
        elif workflow.state == "retrieving_bin":
            await self.transition(workflow, "awaiting_operator")
        else:
            await self.complete_return(workflow, payload.get("weight"))
        await self.publish_workflow(worker, workflow)
        return True

    async def handle_scan(self, code: str, worker: RobotEventPublisher) -> bool:
        # Let the authenticated UI open the return dialog; scanning never moves hardware.
        if not await is_reserved_bin_barcode(code, self.session):
            return False
        await worker.publish_event({"kind": "tray_scanned", "payload": code})
        return True

    async def has_unfinished_motion(self) -> bool:
        # An ordinary outside tray is fine; an interrupted transfer is not a safe auto-home.
        trays = (
            await self.session.scalars(select(StorageBin).where(StorageBin.status == "outside"))
        ).all()
        for tray in trays:
            latest = await self.session.scalar(
                select(StorageBinWorkflow)
                .where(StorageBinWorkflow.bin_id == tray.id)
                .order_by(StorageBinWorkflow.created_at.desc())
                .limit(1)
            )
            if latest and latest.state in {"retrieving_bin", "returning_bin", "failed"}:
                return True
        return False
