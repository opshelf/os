import asyncio
import json
import logging
from typing import Any, Optional

from sqlalchemy import select

import openshelf_os_server.core.robot as robot_core

from openshelf_os.robot_worker import RobotWorker
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.database.models import Telemetry
from openshelf_os_server.database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


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


async def process_robot_event(payload: dict[str, Any]) -> None:
    """
    Handle a single robot event payload, updating storage state when retrieval completes.
    """
    command = payload.get("command")
    message = payload.get("message")

    if command == "retrieve_item":
        if message != "success":
            return

        location = payload.get("location") or {}
        upca = payload.get("upca")

        side = location.get("side")
        shelf_idx = location.get("shelf_idx")
        cell_idx = location.get("cell_idx")

        if upca is None or side is None or shelf_idx is None or cell_idx is None:
            logger.warning("Skipping retrieve_item event with incomplete data: %s", payload)
            return

        try:
            shelf_idx_int = int(shelf_idx)
            cell_idx_int = int(cell_idx)
        except (TypeError, ValueError):
            logger.warning("Skipping retrieve_item event with non-integer location: %s", payload)
            return

        async with AsyncSessionLocal() as session:
            storage_service = StorageService(session)
            try:
                removed = await storage_service.remove_item_location(upca, side, shelf_idx_int, cell_idx_int)
                if removed:
                    logger.info(
                        "Removed location for retrieved item: upca=%s side=%s shelf=%s cell=%s",
                        upca, side, shelf_idx_int, cell_idx_int,
                    )
            except Exception as err:
                logger.exception("Failed to process retrieve_item event for %s: %s", upca, err)

        await flush_telemetry(is_cycle=True)

    if command == "store_item":
        if message != "success":
            return

        location = payload.get("location") or {}
        upca = payload.get("upca")
        weight = payload.get("weight")

        logger.info(
            "Processing store_item event: upca=%s weight=%s location=%s",
            upca, weight, location
        )

        side = location.get("side")
        shelf_idx = location.get("shelf_idx")
        cell_idx = location.get("cell_idx")

        if upca is None or side is None or shelf_idx is None or cell_idx is None:
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

        async with AsyncSessionLocal() as session:
            storage_service = StorageService(session)
            try:
                existing_item = await storage_service.get_item(str(upca))
                logger.info(
                    "Existing item lookup: upca=%s found=%s existing_weight=%s",
                    upca, existing_item is not None,
                    existing_item.weight if existing_item else None
                )
                if existing_item is None:
                    logger.info("Creating new item with weight=%s", normalized_weight)
                    await storage_service.create_item(
                        upca=str(upca),
                        name=str(upca),
                        dimensions=None,
                        weight=normalized_weight,
                        metadata={"name": str(upca)},
                    )
                elif normalized_weight is not None:
                    weights_differ = existing_item.weight != normalized_weight
                    logger.info(
                        "Weight comparison: existing=%s new=%s differ=%s",
                        existing_item.weight, normalized_weight, weights_differ
                    )
                    if weights_differ:
                        logger.info(
                            "Updating item weight: upca=%s old=%s new=%s",
                            upca, existing_item.weight, normalized_weight
                        )
                        result = await storage_service.update_item(str(upca), weight=normalized_weight)
                        logger.info(
                            "Update result: success=%s new_weight=%s",
                            result is not None, result.weight if result else None
                        )
                    else:
                        logger.info("Weights equal, skipping update")
                else:
                    logger.info("Normalized weight is None, skipping update")

                existing_locations = await storage_service.get_item_locations(str(upca))
                already_assigned = any(
                    loc.side == side and loc.shelf_idx == shelf_idx_int and loc.cell_idx == cell_idx_int
                    for loc in existing_locations
                )

                if already_assigned is False:
                    await storage_service.add_item_location(str(upca), side, shelf_idx_int, cell_idx_int)
            except Exception as err:
                logger.exception("Failed to process store_item event for %s: %s", upca, err)

        await flush_telemetry(is_cycle=True)


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

    async def _consume_events(self) -> None:
        if self._queue is None:
            return

        while True:
            payload_json = await self._queue.get()
            logger.info("Received robot event message: %s", payload_json[:200] if payload_json else None)

            try:
                payload = json.loads(payload_json)
            except Exception:
                logger.warning("Received malformed robot event: %s", payload_json)
                continue

            await process_robot_event(payload)
