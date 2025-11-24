import json
import logging
from typing import Any, Optional

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg

from perpetual_os1.common import RobotNatsConfiguration
from perpetual_os1_server.core.storage import StorageService
from perpetual_os1_server.database.session import AsyncSessionLocal


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

        shelf_idx = location.get("shelf_idx")
        cell_idx = location.get("cell_idx")

        if upca is None or shelf_idx is None or cell_idx is None:
            logging.warning("Skipping retrieve_item event with incomplete data: %s", payload)
            return

        try:
            shelf_idx_int = int(shelf_idx)
            cell_idx_int = int(cell_idx)
        except (TypeError, ValueError):
            logging.warning("Skipping retrieve_item event with non-integer location: %s", payload)
            return

        async with AsyncSessionLocal() as session:
            storage_service = StorageService(session)
            try:
                removed = await storage_service.remove_item_location(upca, shelf_idx_int, cell_idx_int)
                if removed:
                    try:
                        await storage_service.sync_to_json(transaction_committed=True)
                    except Exception as sync_err:  # pragma: no cover - best effort logging
                        logging.warning("Failed to sync storage JSON after retrieval: %s", sync_err)
            except Exception as err:
                logging.exception("Failed to process retrieve_item event for %s: %s", upca, err)

    if command == "store_item":
        if message != "success":
            return

        location = payload.get("location") or {}
        upca = payload.get("upca")

        shelf_idx = location.get("shelf_idx")
        cell_idx = location.get("cell_idx")

        if upca is None or shelf_idx is None or cell_idx is None:
            logging.warning("Skipping store_item event with incomplete data: %s", payload)
            return

        try:
            shelf_idx_int = int(shelf_idx)
            cell_idx_int = int(cell_idx)
        except (TypeError, ValueError):
            logging.warning("Skipping store_item event with non-integer location: %s", payload)
            return

        async with AsyncSessionLocal() as session:
            storage_service = StorageService(session)
            try:
                existing_item = await storage_service.get_item(str(upca))
                if existing_item is None:
                    await storage_service.create_item(
                        upca=str(upca),
                        name=str(upca),
                        dimensions=None,
                        weight=None,
                        metadata={"name": str(upca)},
                        sync_to_json=False,
                    )

                existing_locations = await storage_service.get_item_locations(str(upca))
                already_assigned = any(
                    loc.shelf_idx == shelf_idx_int and loc.cell_idx == cell_idx_int
                    for loc in existing_locations
                )

                if already_assigned is False:
                    await storage_service.add_item_location(str(upca), shelf_idx_int, cell_idx_int)

                try:
                    await storage_service.sync_to_json(transaction_committed=True)
                except Exception as sync_err:  # pragma: no cover - best effort logging
                    logging.warning("Failed to sync storage JSON after storage event: %s", sync_err)
            except Exception as err:
                logging.exception("Failed to process store_item event for %s: %s", upca, err)


class RobotEventListener:
    """Subscribe to robot events and update storage state."""

    def __init__(self, nats_config: RobotNatsConfiguration):
        self._config = nats_config
        self._nc: Optional[NATS] = None
        self._subscription = None

    async def start(self) -> None:
        if self._nc is not None:
            return

        self._nc = NATS()
        await self._nc.connect(self._config.connection_string())
        self._subscription = await self._nc.subscribe(
            self._config.event_queue, cb=self._handle_message
        )
        logging.info("Robot event listener subscribed to %s", self._config.event_queue)

    async def stop(self) -> None:
        if self._subscription is not None:
            try:
                await self._subscription.unsubscribe()
            except Exception as err:
                logging.warning("Failed to unsubscribe from robot events: %s", err)
            self._subscription = None

        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception as err:
                logging.warning("Failed to drain NATS connection: %s", err)
            finally:
                try:
                    await self._nc.close()
                except Exception:
                    pass
            self._nc = None

    async def _handle_message(self, msg: Msg) -> None:
        try:
            payload = json.loads(msg.data.decode())
        except Exception:
            logging.warning("Received malformed robot event: %s", msg.data)
            return

        await process_robot_event(payload)
