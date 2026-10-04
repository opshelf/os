from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from openshelf_os.robot.commands import RetrieveItemCommand, StoreItemCommand
from openshelf_os.robot.models import QueuedOperation
from openshelf_os.robot.status import RobotCommandSuccessful
from openshelf_os.robot.worker import RobotWorker


def _bare_worker() -> tuple[RobotWorker, asyncio.Queue[str], AsyncMock]:
	worker = object.__new__(RobotWorker)
	events: asyncio.Queue[str] = asyncio.Queue()
	callback = AsyncMock()
	worker._event_subscribers = [events]
	worker._command_lifecycle_callback = callback
	return worker, events, callback


@pytest.mark.asyncio
async def test_worker_success_event_and_lifecycle_include_command_id() -> None:
	worker, events, callback = _bare_worker()
	command = StoreItemCommand(
		request_id="partner-request",
		item={"barcode": "123456789012", "dimensions": {"width": 1, "height": 1, "length": 1}},
	)
	operation = QueuedOperation(
		command=command,
		timestamp=datetime.now(timezone.utc),
		command_id="10000000-0000-0000-0000-000000000001",
	)

	async def completed():
		return RobotCommandSuccessful(command="store_item", barcode="123456789012")

	task = asyncio.create_task(completed())
	await task
	await worker._publish_completed_command_result(task, command, operation)

	payload = json.loads(await events.get())
	assert payload["message"] == "success"
	assert payload["command_id"] == operation.command_id
	assert payload["request_id"] == "partner-request"
	assert "visibility" not in payload
	callback.assert_awaited_once_with("succeeded", operation, payload)


@pytest.mark.asyncio
async def test_worker_internal_failure_is_normalized_and_marked_internal() -> None:
	worker, events, callback = _bare_worker()
	command = RetrieveItemCommand(
		request_id="bin-workflow-id",
		location={"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
	)
	operation = QueuedOperation(
		command=command,
		timestamp=datetime.now(timezone.utc),
		command_id="10000000-0000-0000-0000-000000000002",
		visibility="internal",
	)
	await worker._publish_command_failure(
		command,
		"Motor stopped",
		operation,
		code="execution_failed",
	)

	payload = json.loads(await events.get())
	assert payload == {
		"command": "retrieve_item",
		"message": "failed",
		"command_id": operation.command_id,
		"error": "Motor stopped",
		"code": "execution_failed",
		"request_id": "bin-workflow-id",
		"barcode": None,
		"location": {"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
		"visibility": "internal",
	}
	callback.assert_awaited_once_with("failed", operation, payload)


@pytest.mark.asyncio
async def test_enqueue_keeps_operation_queued_when_status_publication_fails() -> None:
	worker = object.__new__(RobotWorker)
	worker._command_queue = asyncio.Queue()
	worker._queued_commands = []
	worker._terminalization_lock = asyncio.Lock()
	worker._interruption_generation = 0
	worker._publish_status = AsyncMock(side_effect=RuntimeError("status transport unavailable"))
	command = StoreItemCommand(
		request_id="partner-request",
		item={"barcode": "123456789012", "dimensions": {"width": 1, "height": 1, "length": 1}},
	)

	await worker.enqueue_command(command, command_id="10000000-0000-0000-0000-000000000003")

	queued_operation = await worker._command_queue.get()
	assert queued_operation is worker._queued_commands[0]
	assert queued_operation.command is command
	assert queued_operation.command_id == "10000000-0000-0000-0000-000000000003"
	worker._publish_status.assert_awaited_once_with()
