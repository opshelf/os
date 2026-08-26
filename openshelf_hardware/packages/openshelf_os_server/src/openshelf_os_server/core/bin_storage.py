"""Persistent multi-item bin allocation and scan workflow orchestration."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Optional, cast
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from openshelf_os.robot.commands import RetrieveItemCommand, StoreItemCommand
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
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.settings import get_root_config


ACTIVE_STATES = {
    "retrieving_bin",
    "awaiting_item_scan",
    "awaiting_bin_scan",
    "returning_bin",
    "recovery_required",
    "retry_claimed",
    "reconcile_claimed",
}

RECOVERY_RETRY_CLAIMED = "retry_claimed"
RECOVERY_RECONCILE_CLAIMED = "reconcile_claimed"
RECOVERY_CLAIMED_STATES = {RECOVERY_RETRY_CLAIMED, RECOVERY_RECONCILE_CLAIMED}


# One process-local admission point makes the durable workflow lock and robot
# queue insertion indivisible for this server process. The database unique key
# remains the cross-process backstop for workflow creation.
_command_admission_lock = asyncio.Lock()


@asynccontextmanager
async def bin_workflow_command_admission_guard() -> AsyncIterator[None]:
    async with _command_admission_lock:
        yield


async def ensure_no_active_bin_workflow_for_motion() -> None:
    """Reject an unrelated public motion admission while a bin is checked out."""
    async with AsyncSessionLocal() as session:
        workflow = await BinStorageService(session).active_workflow()
        workflow_id = workflow.id if workflow is not None else None

    if workflow_id is not None:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={
                "message": "A storage bin must be returned before another robot command can run",
                "code": "bin_workflow_active",
                "workflow_id": workflow_id,
            },
        )


async def ensure_worker_queue_empty_for_bin_start(worker: RobotWorker) -> None:
    """Reject a new bin retrieval while any unrelated robot work is active."""
    if get_manual_motion_operation() is not None:
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
    """Return all homes that direct storage must never use."""
    locations = configured_bin_locations()
    rows = (await session.execute(select(StorageBin))).scalars()
    locations.update((row.side, row.shelf_idx, row.cell_idx) for row in rows)
    return locations


async def reserved_bin_barcodes(session: AsyncSession) -> set[str]:
    """Return all bin identities that application-created items cannot claim."""
    rows = (await session.execute(select(StorageBin.barcode))).scalars()
    return configured_bin_barcodes() | set(rows)


async def is_reserved_bin_barcode(barcode: str, session: AsyncSession) -> bool:
    return barcode in await reserved_bin_barcodes(session)


async def is_reserved_bin_location(location: tuple[str, int, int], session: AsyncSession) -> bool:
    return location in await reserved_bin_locations(session)


async def validate_direct_store_reservation(
    item_data: dict[str, Any], session: AsyncSession
) -> None:
    """Reject public direct-store requests that target a bin identity or home."""
    upca = item_data.get("upca")
    if isinstance(upca, str) and await is_reserved_bin_barcode(upca, session):
        raise DirectStoreReservationError(
            "Storage bin barcodes cannot be stored with a direct robot command",
            "reserved_bin_barcode",
        )

    location = _location_from_payload(item_data.get("location"))
    if location is not None and await is_reserved_bin_location(location, session):
        raise DirectStoreReservationError(
            "Storage bin homes cannot be used with a direct robot command",
            "reserved_bin_home",
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
                await self.session.execute(select(Item).where(Item.upca == entry.barcode))
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
                raise ValueError(
                    "Storage bin home "
                    f"{entry.home_location.side}/{entry.home_location.shelf_idx}/{entry.home_location.cell_idx} "
                    "is occupied by a direct item"
                )
            row = existing.get(entry.barcode)
            values = {
                "side": entry.home_location.side,
                "shelf_idx": entry.home_location.shelf_idx,
                "cell_idx": entry.home_location.cell_idx,
                "inner_dimensions": entry.inner_dimensions.model_dump(mode="json"),
                "max_items": entry.max_items,
                "active": True,
            }
            if row is None:
                self.session.add(StorageBin(barcode=entry.barcode, status="at_home", **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)

        for barcode, row in existing.items():
            if barcode not in configured_barcodes:
                member_count = await self._member_count(row.id)
                if member_count == 0 and row.status == "at_home":
                    row.active = False
        await self.session.commit()

    async def recover_interrupted_workflows(self) -> None:
        """Never replay motion after a restart; retain scan-wait states as resumable."""
        rows = (
            (
                await self.session.execute(
                    select(StorageBinWorkflow).where(
                        StorageBinWorkflow.state.in_(
                            {
                                "retrieving_bin",
                                "returning_bin",
                                *RECOVERY_CLAIMED_STATES,
                            }
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.error = "Robot process restarted during motion; physical state must be reconciled"
            row.state = "recovery_required"
            row.recovery_claim = None
            row.history = [*(row.history or []), _history_entry("recovery_required", row.error)]
        if rows:
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

    async def serialize_bin(self, row: StorageBin) -> dict[str, Any]:
        members = (
            (
                await self.session.execute(
                    select(Item)
                    .join(StorageBinItem, StorageBinItem.item_id == Item.id)
                    .where(StorageBinItem.bin_id == row.id)
                    .order_by(Item.name, Item.upca)
                )
            )
            .scalars()
            .all()
        )
        return {
            "id": row.id,
            "barcode": row.barcode,
            "home_location": _location(row),
            "inner_dimensions": row.inner_dimensions,
            "max_items": row.max_items,
            "item_count": len(members),
            "status": row.status,
            "gross_weight": row.gross_weight,
            "active": row.active,
            "items": [{"id": item.id, "upca": item.upca, "name": item.name} for item in members],
        }

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

    async def active_workflow(self, *, for_update: bool = False) -> Optional[StorageBinWorkflow]:
        query = (
            select(StorageBinWorkflow)
            .where(StorageBinWorkflow.state.in_(ACTIVE_STATES))
            .order_by(StorageBinWorkflow.created_at.desc())
            .limit(1)
        )
        if for_update:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def owns_robot_event(self, payload: dict[str, Any]) -> bool:
        """Whether an event is reserved for bin workflow handling, never direct storage."""
        workflow = await self.active_workflow()
        expected_commands = {
            "retrieving_bin": "retrieve_item",
            "returning_bin": "store_item",
        }
        if workflow is not None and (expected_command := expected_commands.get(workflow.state)):
            if (
                payload.get("command") == expected_command
                and payload.get("request_id") == workflow.id
            ):
                return True

        upca = payload.get("upca")
        if isinstance(upca, str) and await is_reserved_bin_barcode(upca, self.session):
            return True

        location = _location_from_payload(payload.get("location"))
        return location is not None and await is_reserved_bin_location(location, self.session)

    async def targets_reserved_bin_identity(self, payload: dict[str, Any]) -> bool:
        """Whether legacy direct mutation would claim a reserved bin barcode or home."""
        upca = payload.get("upca")
        if isinstance(upca, str) and await is_reserved_bin_barcode(upca, self.session):
            return True

        location = _location_from_payload(payload.get("location"))
        return location is not None and await is_reserved_bin_location(location, self.session)

    async def item_has_active_workflow(self, item_id: int) -> bool:
        """Whether an item is the protected target of an unfinished bin workflow."""
        workflows = (
            await self.session.execute(
                select(StorageBinWorkflow).where(StorageBinWorkflow.state.in_(ACTIVE_STATES))
            )
        ).scalars()
        return any(
            workflow.item_id == item_id
            or (
                workflow.operation == "restore"
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

    async def serialize_workflow(self, row: StorageBinWorkflow) -> dict[str, Any]:
        bin_row = await self.session.get(StorageBin, row.bin_id)
        item = await self.session.get(Item, row.item_id)
        if bin_row is None or item is None:
            raise RuntimeError("Bin workflow references missing data")
        payload = {
            "id": row.id,
            "operation": row.operation,
            "state": row.state,
            "bin": await self.serialize_bin(bin_row),
            "item": {"id": item.id, "upca": item.upca, "name": item.name},
            "error": row.error,
            "history": row.history or [],
            "resolution": row.resolution,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }
        if row.operation == "restore":
            candidate_ids = (row.resolution or {}).get("candidate_item_ids", [])
            confirmed_ids = set((row.resolution or {}).get("confirmed_item_ids", []))
            candidates = (
                (
                    await self.session.execute(
                        select(Item).where(Item.id.in_(candidate_ids)).order_by(Item.name, Item.upca)
                    )
                )
                .scalars()
                .all()
                if candidate_ids
                else []
            )
            payload["contents"] = [
                {
                    "id": item.id,
                    "upca": item.upca,
                    "name": item.name,
                    "confirmed": item.id in confirmed_ids,
                }
                for item in candidates
            ]
        return payload

    async def begin_unavailable_return(
        self, barcode: str, user_id: Optional[int] = None
    ) -> Optional[StorageBinWorkflow]:
        """Start a bin-centric return after scanning an unavailable bin."""
        if await self.active_workflow(for_update=True):
            raise BinWorkflowConflict("Another bin workflow is already active")
        bin_row = (
            await self.session.execute(
                select(StorageBin).where(
                    StorageBin.barcode == barcode,
                    StorageBin.active.is_(True),
                    StorageBin.status == "quarantined",
                )
            )
        ).scalar_one_or_none()
        if bin_row is None:
            return None
        previous_workflows = list(
            (
                await self.session.execute(
                    select(StorageBinWorkflow)
                    .where(StorageBinWorkflow.bin_id == bin_row.id)
                    .order_by(StorageBinWorkflow.created_at.asc())
                )
            ).scalars()
        )
        if not previous_workflows:
            raise BinWorkflowConflict("Unavailable bin has no workflow history to reconcile")
        previous = previous_workflows[-1]

        member_ids = set(
            (
                await self.session.execute(
                    select(StorageBinItem.item_id).where(StorageBinItem.bin_id == bin_row.id)
                )
            ).scalars()
        )
        candidate_ids = set(member_ids)
        for prior_workflow in previous_workflows:
            if prior_workflow.operation == "restore":
                candidate_ids.update((prior_workflow.resolution or {}).get("candidate_item_ids", []))
            elif (prior_workflow.resolution or {}).get("resolution") == "quarantined":
                candidate_ids.add(prior_workflow.item_id)
        workflow = StorageBinWorkflow(
            id=str(uuid4()),
            operation="restore",
            state="awaiting_bin_scan",
            lock_key="global",
            bin_id=bin_row.id,
            item_id=previous.item_id,
            user_id=user_id,
            history=[_history_entry("awaiting_bin_scan", "unavailable bin scanned")],
            resolution={
                "candidate_item_ids": sorted(candidate_ids),
                "confirmed_item_ids": sorted(member_ids),
            },
        )
        self.session.add(workflow)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise BinWorkflowConflict("Another bin workflow is already active") from exc
        return workflow

    async def confirm_restore_item(self, workflow: StorageBinWorkflow, code: str) -> bool:
        if workflow.operation != "restore" or workflow.state != "awaiting_bin_scan":
            return False
        candidate_ids = list((workflow.resolution or {}).get("candidate_item_ids", []))
        item = (
            await self.session.execute(
                select(Item).where(Item.upca == code, Item.id.in_(candidate_ids))
            )
        ).scalar_one_or_none()
        if item is None:
            return False
        confirmed_ids = set((workflow.resolution or {}).get("confirmed_item_ids", []))
        confirmed_ids.add(item.id)
        workflow.resolution = {
            **(workflow.resolution or {}),
            "confirmed_item_ids": sorted(confirmed_ids),
        }
        await self.transition(workflow, "awaiting_bin_scan", f"confirmed item {item.upca}")
        return True

    async def cancel_unavailable_return(self, workflow: StorageBinWorkflow) -> None:
        if workflow.operation != "restore" or workflow.state != "awaiting_bin_scan":
            raise BinWorkflowConflict("Unavailable bin return cannot be closed now")
        workflow.resolution = {**(workflow.resolution or {}), "resolution": "cancelled"}
        await self.transition(workflow, "reconciled", "operator closed unavailable bin return")

    @staticmethod
    def _fits(item: Item, bin_row: StorageBin) -> bool:
        dims = item.dimensions or {}
        inside = bin_row.inner_dimensions or {}
        try:
            width, height, length = (float(dims[key]) for key in ("width", "height", "length"))
            inner_width, inner_height, inner_length = (
                float(inside[key]) for key in ("width", "height", "length")
            )
        except (KeyError, TypeError, ValueError):
            return False
        return height <= inner_height and (
            (width <= inner_width and length <= inner_length)
            or (length <= inner_width and width <= inner_length)
        )

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
            if len(members) >= row.max_items or not self._fits(item, row):
                continue
            if any(member.access_level_id != item.access_level_id for member in members):
                continue
            inside = row.inner_dimensions
            volume = float(inside["width"]) * float(inside["height"]) * float(inside["length"])
            candidates.append(
                (len(members) == 0, volume, row.side, row.shelf_idx, row.cell_idx, row)
            )
        if not candidates:
            return None
        return min(candidates, key=lambda entry: entry[:-1])[-1]

    async def begin(
        self, operation: str, item: Item, user_id: Optional[int]
    ) -> Optional[StorageBinWorkflow]:
        if await self.active_workflow(for_update=True):
            raise BinWorkflowConflict("Another bin workflow is already active")
        if operation == "retrieve":
            bin_row = await self.item_bin(item.id)
            if bin_row is None:
                return None
            if bin_row.status != "at_home":
                raise BinWorkflowConflict(
                    f"Storage bin is {bin_row.status} and cannot be retrieved"
                )
        elif operation == "store":
            if await self.item_bin(item.id):
                raise BinWorkflowConflict("Item is already stored in a bin")
            bin_row = await self.allocate_bin(item)
            if bin_row is None:
                return None
        else:
            raise ValueError("operation must be 'store' or 'retrieve'")

        workflow = StorageBinWorkflow(
            id=str(uuid4()),
            operation=operation,
            state="retrieving_bin",
            lock_key="global",
            bin_id=bin_row.id,
            item_id=item.id,
            user_id=user_id,
            history=[_history_entry("retrieving_bin")],
        )
        bin_row.status = "checked_out"
        self.session.add(workflow)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise BinWorkflowConflict("Another bin workflow is already active") from exc
        return workflow

    async def transition(
        self, workflow: StorageBinWorkflow, state: str, detail: Optional[str] = None
    ) -> None:
        workflow.state = state
        if state not in RECOVERY_CLAIMED_STATES:
            workflow.recovery_claim = None
        workflow.history = [*(workflow.history or []), _history_entry(state, detail)]
        workflow.updated_at = _utc_now()
        if state in {"completed", "reconciled"}:
            workflow.completed_at = _utc_now()
            workflow.lock_key = None
        await self.session.commit()

    async def claim_recovery_workflow(
        self,
        workflow_id: str,
        claim_state: str,
        *,
        source_states: tuple[str, ...] = ("recovery_required",),
    ) -> Optional[StorageBinWorkflow]:
        """Atomically claim an eligible workflow for one retry or reconciliation."""
        if claim_state not in RECOVERY_CLAIMED_STATES:
            raise ValueError("claim_state must be a recovery claim state")
        claim = str(uuid4())
        result = await self.session.execute(
            update(StorageBinWorkflow)
            .where(
                StorageBinWorkflow.id == workflow_id,
                StorageBinWorkflow.state.in_(source_states),
                StorageBinWorkflow.recovery_claim.is_(None),
            )
            .values(state=claim_state, recovery_claim=claim, updated_at=_utc_now())
        )
        if getattr(result, "rowcount", 0) != 1:
            await self.session.rollback()
            return None
        await self.session.commit()
        return await self.session.get(StorageBinWorkflow, workflow_id, populate_existing=True)

    async def transition_recovery_claim(
        self,
        workflow: StorageBinWorkflow,
        *,
        expected_state: str,
        claim: str,
        state: str,
        detail: Optional[str] = None,
        retain_claim: bool = False,
    ) -> bool:
        """Conditionally advance or release a durable recovery claim."""
        history = [*(workflow.history or []), _history_entry(state, detail)]
        values: dict[str, Any] = {
            "state": state,
            "history": history,
            "updated_at": _utc_now(),
            "recovery_claim": claim if retain_claim else None,
        }
        if state in {"completed", "reconciled"}:
            values["completed_at"] = _utc_now()
            values["lock_key"] = None
        await self.session.flush()
        result = await self.session.execute(
            update(StorageBinWorkflow)
            .where(
                StorageBinWorkflow.id == workflow.id,
                StorageBinWorkflow.state == expected_state,
                StorageBinWorkflow.recovery_claim == claim,
            )
            .values(**values)
        )
        if getattr(result, "rowcount", 0) != 1:
            await self.session.rollback()
            return False
        await self.session.commit()
        await self.session.refresh(workflow)
        return True

    async def release_recovery_claim(
        self,
        workflow: StorageBinWorkflow,
        *,
        expected_state: str,
        claim: str,
    ) -> bool:
        """Release an unused claim without changing recovery diagnostics."""
        result = await self.session.execute(
            update(StorageBinWorkflow)
            .where(
                StorageBinWorkflow.id == workflow.id,
                StorageBinWorkflow.state == expected_state,
                StorageBinWorkflow.recovery_claim == claim,
            )
            .values(state="recovery_required", recovery_claim=None, updated_at=_utc_now())
        )
        if getattr(result, "rowcount", 0) != 1:
            await self.session.rollback()
            return False
        await self.session.commit()
        await self.session.refresh(workflow)
        return True

    async def clear_recovery_claim(
        self,
        workflow: StorageBinWorkflow,
        *,
        expected_state: str,
        claim: str,
    ) -> bool:
        """Clear a successful retry's ownership marker without changing its state."""
        result = await self.session.execute(
            update(StorageBinWorkflow)
            .where(
                StorageBinWorkflow.id == workflow.id,
                StorageBinWorkflow.state == expected_state,
                StorageBinWorkflow.recovery_claim == claim,
            )
            .values(recovery_claim=None, updated_at=_utc_now())
        )
        if getattr(result, "rowcount", 0) != 1:
            await self.session.rollback()
            return False
        await self.session.commit()
        await self.session.refresh(workflow)
        return True

    @staticmethod
    def physical_return_completed(workflow: StorageBinWorkflow) -> bool:
        """Whether a successful store motion has durably returned the bin home."""
        return any(
            entry.get("state") == "physical_return_completed" for entry in workflow.history or []
        )

    async def mark_physical_return_completed(self, workflow: StorageBinWorkflow) -> None:
        """Persist return completion before finalizing item membership."""
        if self.physical_return_completed(workflow):
            return
        workflow.history = [*(workflow.history or []), _history_entry("physical_return_completed")]
        workflow.updated_at = _utc_now()
        await self.session.commit()

    async def start_retrieval(self, workflow: StorageBinWorkflow, worker: RobotWorker) -> None:
        ensure_worker_homed_for_bin_motion(worker)
        bin_row = await self.session.get(StorageBin, workflow.bin_id)
        if bin_row is None:
            raise RuntimeError("Configured bin no longer exists")
        await worker.enqueue_command(
            RetrieveItemCommand(
                request_id=workflow.id,
                upca=bin_row.barcode,
                location=_location(bin_row),
            )
        )

    async def _publish(
        self,
        worker: RobotWorker,
        workflow: StorageBinWorkflow,
        *,
        rejected_scan: Optional[str] = None,
    ) -> None:
        event: dict[str, Any] = {
            "kind": "bin_workflow_updated",
            "payload": await self.serialize_workflow(workflow),
        }
        if rejected_scan:
            event["kind"] = "bin_workflow_scan_rejected"
            event["rejected_scan"] = rejected_scan
        await worker.publish_event(event)

    async def publish_workflow(self, worker: RobotWorker, workflow: StorageBinWorkflow) -> None:
        await self._publish(worker, workflow)

    async def handle_robot_result(self, payload: dict[str, Any], worker: RobotWorker) -> bool:
        workflow = await self.active_workflow()
        if workflow is None or workflow.state not in {"retrieving_bin", "returning_bin"}:
            return False
        bin_row = await self.session.get(StorageBin, workflow.bin_id)
        expected_command = "retrieve_item" if workflow.state == "retrieving_bin" else "store_item"
        if bin_row is None or payload.get("command") != expected_command:
            return False
        if payload.get("request_id") != workflow.id:
            return False
        if payload.get("upca") not in {None, bin_row.barcode}:
            return False
        message = payload.get("message")
        if message != "success":
            workflow.error = str(payload.get("error") or "Robot command failed")
            await self.transition(workflow, "recovery_required", workflow.error)
            await self._publish(worker, workflow)
            return True
        if workflow.state == "retrieving_bin":
            await self.transition(workflow, "awaiting_item_scan")
            await self._publish(worker, workflow)
            return True

        await self.complete_return(workflow, payload.get("weight"))
        await self._publish(worker, workflow)
        return True

    async def complete_return(self, workflow: StorageBinWorkflow, gross_weight: Any = None) -> None:
        if workflow.state == "returning_bin":
            # A later invariant failure must not permit another StoreItemCommand:
            # the bin is already physically at its home location.
            await self.mark_physical_return_completed(workflow)
        bin_row = await self.session.get(StorageBin, workflow.bin_id)
        item = await self.session.get(Item, workflow.item_id)
        if bin_row is None:
            raise RuntimeError("Workflow bin no longer exists")
        if item is None:
            raise RuntimeError("Workflow item no longer exists")
        if workflow.operation == "restore":
            completion_error = await self._restore_completion_error(workflow, bin_row)
            if completion_error is not None:
                workflow.error = completion_error
                await self.transition(workflow, "recovery_required", completion_error)
                return
            await self._apply_restore_contents(workflow, bin_row)
        else:
            membership = (
                await self.session.execute(
                    select(StorageBinItem).where(StorageBinItem.item_id == item.id)
                )
            ).scalar_one_or_none()
            target_item_in_bin = (workflow.resolution or {}).get("target_item_in_bin")
            if not isinstance(target_item_in_bin, bool):
                # Compatibility for workflows started before the return confirmation was introduced.
                target_item_in_bin = workflow.operation == "store"

            if target_item_in_bin:
                completion_error = None
                if membership is not None and membership.bin_id != bin_row.id:
                    completion_error = "Item is already associated with another storage bin"
                elif membership is None:
                    completion_error = await self._store_completion_error(workflow, bin_row, item)
                if completion_error is not None:
                    workflow.error = completion_error
                    await self.transition(workflow, "recovery_required", completion_error)
                    return
                if membership is None:
                    self.session.add(StorageBinItem(bin_id=bin_row.id, item_id=item.id))
            elif membership is not None:
                await self.session.delete(membership)
        bin_row.status = "at_home"
        if gross_weight is not None:
            try:
                bin_row.gross_weight = float(gross_weight)
            except (TypeError, ValueError):
                pass
        await self.transition(workflow, "completed")

    async def _apply_restore_contents(
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
        for item_id in confirmed_ids - current_ids:
            self.session.add(StorageBinItem(bin_id=bin_row.id, item_id=item_id))

    async def _restore_completion_error(
        self, workflow: StorageBinWorkflow, bin_row: StorageBin
    ) -> Optional[str]:
        candidate_ids = set((workflow.resolution or {}).get("candidate_item_ids", []))
        confirmed_ids = set((workflow.resolution or {}).get("confirmed_item_ids", []))
        if not confirmed_ids.issubset(candidate_ids):
            return "Confirmed bin contents include an unexpected item"
        items = (
            (
                await self.session.execute(select(Item).where(Item.id.in_(confirmed_ids)))
            )
            .scalars()
            .all()
            if confirmed_ids
            else []
        )
        if len(items) != len(confirmed_ids):
            return "A confirmed bin item no longer exists"
        if len(items) > bin_row.max_items:
            return "Storage bin has reached its item limit"
        if any(not self._fits(item, bin_row) for item in items):
            return "A confirmed item no longer fits the configured storage bin"
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
        return None

    async def _store_completion_error(
        self,
        workflow: StorageBinWorkflow,
        bin_row: StorageBin,
        item: Item,
    ) -> Optional[str]:
        """Validate the durable state before associating a returned item with a bin."""
        active_workflow = await self.active_workflow()
        if active_workflow is None or active_workflow.id != workflow.id:
            return "Bin workflow is no longer the active workflow"
        if workflow.state != "returning_bin" or bin_row.status != "checked_out":
            return "Bin workflow is not returning its checked-out bin"
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
        if any(member.access_level_id != item.access_level_id for member in members):
            return "Item access level no longer matches the storage bin"
        return None

    async def handle_mock_scan(self, workflow: StorageBinWorkflow, code: str) -> bool:
        """Deterministic scanner/scale behavior for the database-backed mock runtime."""
        if workflow.operation == "restore":
            return await self.confirm_restore_item(workflow, code)
        item = await self.session.get(Item, workflow.item_id)
        bin_row = await self.session.get(StorageBin, workflow.bin_id)
        if item is None or bin_row is None:
            raise RuntimeError("Bin workflow references missing data")
        expected = item.upca if workflow.state == "awaiting_item_scan" else bin_row.barcode
        if code != expected:
            return False
        if workflow.state == "awaiting_item_scan":
            await self.transition(workflow, "awaiting_bin_scan")
            return True
        if workflow.state == "awaiting_bin_scan":
            return True
        return False

    async def handle_scan(self, code: str, worker: RobotWorker) -> bool:
        workflow = await self.active_workflow()
        if workflow is None:
            workflow = await self.begin_unavailable_return(code)
            if workflow is None:
                return False
            await self._publish(worker, workflow)
            return True
        if workflow.operation == "restore":
            accepted = await self.confirm_restore_item(workflow, code)
            await self._publish(worker, workflow, rejected_scan=None if accepted else code)
            return True
        if workflow.state not in {"awaiting_item_scan", "awaiting_bin_scan"}:
            return False
        item = await self.session.get(Item, workflow.item_id)
        bin_row = await self.session.get(StorageBin, workflow.bin_id)
        if item is None or bin_row is None:
            return False
        expected = item.upca if workflow.state == "awaiting_item_scan" else bin_row.barcode
        if code != expected:
            await self._publish(worker, workflow, rejected_scan=code)
            return True
        if workflow.state == "awaiting_item_scan":
            await self.transition(workflow, "awaiting_bin_scan")
            await self._publish(worker, workflow)
            return True

        # A bin scan identifies the expected container but must never start
        # physical motion. Returning the bin requires an explicit Store action.
        await self._publish(worker, workflow)
        return True

    async def start_return(
        self,
        workflow: StorageBinWorkflow,
        worker: RobotWorker,
        *,
        command_admitted: bool = False,
    ) -> None:
        """Start the physical bin return after explicit operator confirmation."""
        if workflow.state != "awaiting_bin_scan":
            raise BinWorkflowConflict("Bin workflow is not ready to return")

        occupied = False
        cached = worker.get_cached_scale_status()
        if cached is not None:
            occupied = cached.occupied is True
        else:
            try:
                occupied = abs(float((await worker.get_scale_data())["weight"])) > 5.0
            except Exception:
                occupied = False
        if not occupied:
            raise BinWorkflowConflict("Place the bin in the IO box before storing it")

        ensure_worker_homed_for_bin_motion(worker)

        await self.transition(workflow, "returning_bin")
        await self._publish(worker, workflow)
        try:
            await self.enqueue_return(workflow, worker, command_admitted=command_admitted)
        except Exception as exc:
            workflow.error = str(exc)
            await self.transition(workflow, "recovery_required", workflow.error)
            await self._publish(worker, workflow)

    async def enqueue_return(
        self, workflow: StorageBinWorkflow, worker: RobotWorker, *, command_admitted: bool = False
    ) -> None:
        ensure_worker_homed_for_bin_motion(worker)
        bin_row = await self.session.get(StorageBin, workflow.bin_id)
        if bin_row is None:
            raise RuntimeError("Configured bin no longer exists")
        command = StoreItemCommand(
            request_id=workflow.id,
            item={
                "upca": bin_row.barcode,
                "name": f"Storage bin {bin_row.barcode}",
                "dimensions": bin_row.inner_dimensions,
                "location": _location(bin_row),
            },
        )
        if command_admitted:
            await worker.enqueue_command(command)
        else:
            async with bin_workflow_command_admission_guard():
                await worker.enqueue_command(command)

    async def reconcile(
        self,
        workflow: StorageBinWorkflow,
        *,
        resolution: str,
        target_item_in_bin: Optional[bool],
        recovery_claim: Optional[str] = None,
    ) -> bool:
        if resolution not in {"returned", "quarantined"}:
            raise ValueError("resolution must be returned or quarantined")
        bin_row = await self.session.get(StorageBin, workflow.bin_id)
        if resolution == "returned":
            membership = (
                await self.session.execute(
                    select(StorageBinItem).where(StorageBinItem.item_id == workflow.item_id)
                )
            ).scalar_one_or_none()
            if workflow.operation == "restore" and bin_row is not None:
                await self._apply_restore_contents(workflow, bin_row)
            else:
                if target_item_in_bin is None:
                    raise ValueError("target item state is required for a returned bin")
                if target_item_in_bin and membership is None:
                    self.session.add(StorageBinItem(bin_id=workflow.bin_id, item_id=workflow.item_id))
                if not target_item_in_bin and membership is not None:
                    await self.session.delete(membership)
        if bin_row:
            bin_row.status = "at_home" if resolution == "returned" else "quarantined"
        if workflow.operation == "restore":
            workflow.resolution = {**(workflow.resolution or {}), "resolution": resolution}
        elif resolution == "returned":
            workflow.resolution = {
                "resolution": resolution,
                "target_item_in_bin": target_item_in_bin,
            }
        else:
            workflow.resolution = {"resolution": resolution}
        workflow.error = None
        if recovery_claim is not None:
            return await self.transition_recovery_claim(
                workflow,
                expected_state=RECOVERY_RECONCILE_CLAIMED,
                claim=recovery_claim,
                state="reconciled",
                detail=resolution,
            )
        await self.transition(workflow, "reconciled", resolution)
        return True

    async def cancel_before_motion(self, workflow: StorageBinWorkflow, reason: str) -> None:
        bin_row = await self.session.get(StorageBin, workflow.bin_id)
        if bin_row is not None:
            bin_row.status = "at_home"
        workflow.resolution = {"resolution": "cancelled", "reason": reason}
        workflow.error = reason
        await self.transition(workflow, "reconciled", reason)


async def quarantined_bin_cells(session: AsyncSession) -> list[tuple[str, int, int]]:
    rows = (
        (
            await session.execute(
                select(StorageBin).where(
                    StorageBin.active.is_(True), StorageBin.status == "quarantined"
                )
            )
        )
        .scalars()
        .all()
    )
    return [(row.side, row.shelf_idx, row.cell_idx) for row in rows]
