"""Broadcast inventory changes that do not come from robot command results."""
import logging
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from openshelf_os.common import OperatingMode

from ..core import robot as robot_core
from ..core.mock_robot import get_mock_robot_runtime
from ..settings import get_operating_mode

INVENTORY_CHANGED_EVENT = {"kind": "inventory_changed"}
INVENTORY_PATH_PREFIX = "/api/storage/"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
READ_ONLY_PATHS = {"/api/storage/items/lookup"}

logger = logging.getLogger(__name__)


async def publish_inventory_changed() -> None:
	"""Tell every connected client on the robot events stream to refetch inventory."""
	try:
		if get_operating_mode() == OperatingMode.mock:
			await get_mock_robot_runtime().publish_event(INVENTORY_CHANGED_EVENT)
			return
		worker = robot_core.robot_worker
		if worker is not None:
			await worker.publish_event(INVENTORY_CHANGED_EVENT)
	except Exception as exc:
		logger.warning("Failed to publish inventory change: %s", exc)


def is_inventory_mutation(request: Request, response: Response) -> bool:
	if request.method not in MUTATING_METHODS:
		return False
	if request.url.path.startswith(INVENTORY_PATH_PREFIX) is False:
		return False
	if request.url.path in READ_ONLY_PATHS:
		return False
	return 200 <= response.status_code < 300


class InventoryEventsMiddleware(BaseHTTPMiddleware):
	"""Publish `inventory_changed` after successful storage writes.

	Robot results already reach every client on the events stream; edits made through the
	storage API (create, update, delete, location and grid edits, bin workflows) did not, so
	other open UIs kept showing stale inventory until they reloaded.
	"""

	async def dispatch(self, request: Request, call_next: Callable) -> Response:
		response: Response = await call_next(request)
		if is_inventory_mutation(request, response) is True:
			await publish_inventory_changed()
		return response
