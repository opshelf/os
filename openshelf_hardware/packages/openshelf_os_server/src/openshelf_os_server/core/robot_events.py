import asyncio
import json
import logging
from typing import Any, Optional

from sqlalchemy import select

import openshelf_os_server.core.robot as robot_core

from openshelf_os.robot.worker import RobotWorker
from openshelf_os_server.core.event_log import append_robot_event
from openshelf_os_server.core.mass import apply_cycle_mass
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.core.bin_storage import BinStorageService, RobotEventPublisher
from openshelf_os_server.database.models import Telemetry
from openshelf_os_server.database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)
EVENT_DRAIN_TIMEOUT_S = 5.0
EVENT_FORWARD_WAIT_TIMEOUT_S = 5.0


class EventProcessingTracker:
    """Track worker events whose side effects have finished.

    The event listener commits DB state after the worker emits an event, so the
    durable event log row (which commits after the side effects) only exists once
    processing has finished. The tracker marks the release point after the log
    append, so side-effect completeness is tracked for concurrent operations.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Condition()
        self._processed = 0
        self._active = False

    def activate(self) -> None:
        self._active = True

    def deactivate(self) -> None:
        self._active = False



    async def record_processed(self) -> None:
        async with self._lock:
            self._processed += 1
            self._lock.notify_all()


event_processing_tracker = EventProcessingTracker()


async def _refresh_status_after_commit(worker: Optional[RobotEventPublisher]) -> None:
    """Push a fresh status snapshot so SSE overlays reflect committed DB state.

    The status overlay re-reads item locations from the database on the first
    push after a commit (`core.occupancy_cache` serves the last snapshot until a
    commit bumps its generation), so a push issued after the commit corrects the
    stale occupancy published alongside the pre-commit state change.
    """
    if worker is None:
        return
    snapshot = getattr(worker, "get_status_snapshot", None)
    if snapshot is None:
        return
    try:
        await snapshot(force_refresh=True)
    except Exception as err:
        logger.warning("Failed to refresh robot status after commit: %s", err)


async def flush_telemetry(is_cycle: bool) -> None:
    motor_seconds = 0.0
    if robot_core.robot_worker is not None:
        motor_seconds = robot_core.robot_worker.drain_motor_time()

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Telemetry).where(Telemetry.id == 1))
            row = result.scalar_one_or_none()

            if row is None:
                row = Telemetry(
                    id=1,
                    cycle_count=1 if is_cycle is True else 0,
                    motor_movement_seconds=motor_seconds,
                )
                session.add(row)
            else:
                if is_cycle is True:
                    row.cycle_count += 1
                row.motor_movement_seconds += motor_seconds

            await session.commit()
    except Exception as err:
        logger.warning("Failed to flush telemetry: %s", err)


async def process_robot_event(
    payload: dict[str, Any], worker: Optional[RobotEventPublisher] = None
) -> None:
    """
    Handle a single robot event payload, updating storage state when retrieval completes.
    """
    active_worker = worker or robot_core.robot_worker
    if active_worker is not None:
        bin_owned = False
        try:
            async with AsyncSessionLocal() as session:
                bin_service = BinStorageService(session)
                bin_owned = await bin_service.owns_robot_event(payload)
                if payload.get("kind") == "label_scanned":
                    if await bin_service.handle_scan(str(payload.get("payload", "")), active_worker):
                        return
                elif await bin_service.handle_robot_result(payload, active_worker):
                    if (
                        payload.get("command") == "store_item"
                        and payload.get("message") == "success"
                    ):
                        await flush_telemetry(is_cycle=True)
                    return
        except Exception as err:
            logger.exception("Failed to process bin workflow event: %s", err)

        if bin_owned:
            logger.warning("Skipping legacy mutation for bin-owned robot event: %s", payload)
            return

    command = payload.get("command")
    message = payload.get("message")

    if command == "retrieve_item":
        if message != "success":
            return

        location = payload.get("location") or {}
        barcode = payload.get("barcode")

        side = location.get("side")
        shelf_idx = location.get("shelf_idx")
        cell_idx = location.get("cell_idx")

        if barcode is None or side is None or shelf_idx is None or cell_idx is None:
            logger.warning("Skipping retrieve_item event with incomplete data: %s", payload)
            return

        try:
            shelf_idx_int = int(shelf_idx)
            cell_idx_int = int(cell_idx)
        except (TypeError, ValueError):
            logger.warning("Skipping retrieve_item event with non-integer location: %s", payload)
            return

        removed = False
        async with AsyncSessionLocal() as session:
            if await BinStorageService(session).targets_reserved_bin_identity(payload):
                logger.warning(
                    "Skipping legacy mutation for reserved bin retrieve event: %s", payload
                )
                return
            storage_service = StorageService(session)
            try:
                item = await storage_service.get_item(str(barcode))
                if item is not None:
                    if not apply_cycle_mass(item, payload.get("weight"), stored=False, command_id=payload.get("command_id")):
                        return
                removed = await storage_service.remove_item_location(barcode, side, shelf_idx_int, cell_idx_int, commit=False)
                await session.commit()
                if removed:
                    logger.info(
                        "Removed location for retrieved item: barcode=%s side=%s shelf=%s cell=%s",
                        barcode, side, shelf_idx_int, cell_idx_int,
                    )
            except Exception as err:
                logger.exception("Failed to process retrieve_item event for %s: %s", barcode, err)

        await flush_telemetry(is_cycle=True)
        if removed:
            await _refresh_status_after_commit(active_worker)

    if command == "store_item":
        if message != "success":
            return

        location = payload.get("location") or {}
        barcode = payload.get("barcode")
        weight = payload.get("weight")

        logger.info(
            "Processing store_item event: barcode=%s weight=%s location=%s",
            barcode, weight, location
        )

        side = location.get("side")
        shelf_idx = location.get("shelf_idx")
        cell_idx = location.get("cell_idx")

        if barcode is None or side is None or shelf_idx is None or cell_idx is None:
            logger.warning("Skipping store_item event with incomplete data: %s", payload)
            return

        try:
            shelf_idx_int = int(shelf_idx)
            cell_idx_int = int(cell_idx)
        except (TypeError, ValueError):
            logger.warning("Skipping store_item event with non-integer location: %s", payload)
            return

        normalized_weight = None
        if weight is not None:
            try:
                normalized_weight = float(weight)
                if normalized_weight <= 0:
                    logger.warning(
                        "Weight normalization: raw=%s normalized to None (<=0)",
                        weight
                    )
                    normalized_weight = None
            except (TypeError, ValueError):
                logger.warning(
                    "Weight normalization failed: raw=%s type=%s",
                    weight, type(weight).__name__
                )
                normalized_weight = None

        logger.info(
            "Weight after normalization: raw=%s normalized=%s",
            weight, normalized_weight
        )

        location_added = False
        async with AsyncSessionLocal() as session:
            if await BinStorageService(session).targets_reserved_bin_identity(payload):
                logger.warning("Skipping legacy mutation for reserved bin store event: %s", payload)
                return
            storage_service = StorageService(session)
            try:
                existing_item = await storage_service.get_item(str(barcode))
                logger.info(
                    "Existing item lookup: barcode=%s found=%s existing_weight=%s",
                    barcode, existing_item is not None,
                    existing_item.weight if existing_item else None
                )
                if existing_item is None:
                    logger.info("Creating new item with weight=%s", normalized_weight)
                    existing_item = await storage_service.create_item(
                        barcode=str(barcode),
                        name=str(barcode),
                        dimensions=None,
                        weight=normalized_weight,
                        metadata={"name": str(barcode)},
                    )
                if not apply_cycle_mass(existing_item, payload.get("weight"), stored=True, command_id=payload.get("command_id")):
                    return

                existing_locations = await storage_service.get_item_locations(str(barcode))
                already_assigned = any(
                    loc.side == side and loc.shelf_idx == shelf_idx_int and loc.cell_idx == cell_idx_int
                    for loc in existing_locations
                )

                if already_assigned is False:
                    await storage_service.add_item_location(str(barcode), side, shelf_idx_int, cell_idx_int, commit=False)
                    location_added = True
                await session.commit()
            except Exception as err:
                logger.exception("Failed to process store_item event for %s: %s", barcode, err)

        await flush_telemetry(is_cycle=True)
        if location_added:
            await _refresh_status_after_commit(active_worker)


class RobotEventListener:
    """Subscribe to worker events and update storage state."""

    def __init__(self, worker: RobotWorker):
        self._worker = worker
        self._queue: Optional[asyncio.Queue[str]] = None
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        if self._task is not None:
            logger.info("Robot event listener already started")
            return

        logger.info("Starting robot event listener")
        self._queue = await self._worker.subscribe_events()
        self._task = asyncio.create_task(self._consume_events(), name="robot-event-listener")
        event_processing_tracker.activate()

    async def stop(self) -> None:
        if self._queue is not None:
            try:
                await self._worker.unsubscribe_events(self._queue)
            except Exception as err:
                logger.warning("Failed to unsubscribe from robot events: %s", err)
            self._queue = None

        if self._task is not None:
            self._task.cancel()

            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as err:
                logger.warning("Failed to stop robot event listener task: %s", err)

            self._task = None

        event_processing_tracker.deactivate()

    async def drain(self) -> None:
        """Wait briefly for events already emitted by the worker to be persisted."""
        queue = self._queue
        if queue is None:
            return

        try:
            await asyncio.wait_for(queue.join(), timeout=EVENT_DRAIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out draining robot events after %.1fs; stopping listener",
                EVENT_DRAIN_TIMEOUT_S,
            )

    async def _consume_events(self) -> None:
        queue = self._queue
        if queue is None:
            return

        while True:
            payload_json = await queue.get()
            try:
                logger.info("Received robot event message: %s", payload_json[:200] if payload_json else None)

                try:
                    payload = json.loads(payload_json)
                except Exception:
                    logger.warning("Received malformed robot event: %s", payload_json)
                    continue

                await process_robot_event(payload, self._worker)
                # The log row commits before the tracker release, so SSE clients
                # can only observe the event once it is durable and replayable.
                await append_robot_event(payload)
            finally:
                queue.task_done()
                await event_processing_tracker.record_processed()
