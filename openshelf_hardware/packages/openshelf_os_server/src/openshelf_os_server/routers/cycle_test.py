from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import Response
from sse_starlette.sse import EventSourceResponse

from openshelf_os_server.core.cycle_test import (
	TERMINAL_STATES,
	CycleTestConfig,
	CycleTestSnapshot,
	CycleTestStartError,
	get_cycle_test_manager,
)
from openshelf_os_server.database.models import User
from openshelf_os_server.documentation import response_docs, stream_docs
from openshelf_os_server.errors import api_error
from openshelf_os_server.routers.auth_deps import get_current_user_for_stream, require_permissions_for_stream

# Stream-safe auth for the same reason as the calibration router: /stream stays open for the run.
router = APIRouter(
	prefix="/api/calibration/cycle-test",
	tags=["calibration"],
	dependencies=[Depends(get_current_user_for_stream)],
)

ROBOT_CONTROL = Depends(require_permissions_for_stream("robot:control"))


@router.post(
	"",
	dependencies=[ROBOT_CONTROL],
	response_model=CycleTestSnapshot,
	summary="Start a store/retrieve cycle test",
	responses=response_docs(errors=(401, 403, 409, 422)),
)
async def start_cycle_test(
	config: CycleTestConfig,
	request: Request,
	current_user: User = Depends(get_current_user_for_stream),
) -> CycleTestSnapshot:
	"""Start a store/retrieve cycle test.

	Server-side equivalent of `provision/scripts/cycle_test.py`: walks the enabled storage
	cells, creating test items and running real store/retrieve commands (or calibration
	quick tests) through this API as the calling user, and stops at the first failure or
	hardware fault. Runs only in development or mock mode because it creates and deletes
	items; `clear` deletes every item first. Follow progress on `GET /stream`. Returns `409`
	while another cycle test runs.
	"""
	try:
		return await get_cycle_test_manager().start(request.app, current_user, config)
	except CycleTestStartError as exc:
		raise api_error(status.HTTP_409_CONFLICT, exc.code, exc.message) from exc


@router.get(
	"",
	response_model=CycleTestSnapshot,
	summary="Read the cycle test state",
	responses=response_docs(errors=(401,)),
)
async def get_cycle_test() -> CycleTestSnapshot:
	"""Read the cycle test state.

	Returns the current or most recent run with its configuration, per-cell results of the
	current loop and the log tail; `state` is `idle` until the first run on this process.
	"""
	return CycleTestSnapshot.model_validate_json(get_cycle_test_manager().serialize())


@router.get(
	"/stream",
	summary="Stream cycle test progress",
	responses=response_docs(
		errors=(401,),
		existing=stream_docs(
			"text/event-stream",
			"Full cycle test snapshots encoded as JSON in `snapshot` SSE frames. Never starts a run.",
		),
	),
	response_class=Response,
)
async def stream_cycle_test(request: Request) -> EventSourceResponse:
	"""Stream cycle test progress.

	Sends the current snapshot at once and a fresh one on every change; the stream ends
	after a terminal snapshot. Closing it does not stop the run; use the abort endpoint.
	"""
	manager = get_cycle_test_manager()
	queue, initial = manager.subscribe()

	async def event_generator():
		try:
			yield {"event": "snapshot", "data": initial}
			if manager.is_active() is False:
				return
			while True:
				if await request.is_disconnected() is True:
					break
				try:
					payload = await asyncio.wait_for(queue.get(), timeout=1.0)
				except asyncio.TimeoutError:
					continue
				yield {"event": "snapshot", "data": payload}
				if CycleTestSnapshot.model_validate_json(payload).state in TERMINAL_STATES and manager.is_active() is False:
					break
		finally:
			manager.unsubscribe(queue)

	return EventSourceResponse(event_generator(), ping=15, send_timeout=30, headers={"Cache-Control": "no-cache"})


@router.post(
	"/continue",
	dependencies=[ROBOT_CONTROL],
	response_model=CycleTestSnapshot,
	summary="Continue a paused cycle test",
	responses=response_docs(errors=(401, 403, 409)),
)
async def continue_cycle_test() -> CycleTestSnapshot:
	"""Continue a paused cycle test.

	Step mode pauses after every cell; this moves on to the next one. Returns `409` when the
	run is not paused.
	"""
	manager = get_cycle_test_manager()
	if manager.resume() is False:
		raise api_error(status.HTTP_409_CONFLICT, "cycle_test_not_paused", "The cycle test is not paused.")
	return CycleTestSnapshot.model_validate_json(manager.serialize())


@router.post(
	"/abort",
	dependencies=[ROBOT_CONTROL],
	response_model=CycleTestSnapshot,
	summary="Abort the cycle test",
	responses=response_docs(errors=(401, 403, 409)),
)
async def abort_cycle_test() -> CycleTestSnapshot:
	"""Abort the cycle test.

	Stops before the next cell; the cell in progress finishes its current command first so
	no item is left mid-transfer. Returns `409` when no cycle test is running.
	"""
	manager = get_cycle_test_manager()
	if manager.abort() is False:
		raise api_error(status.HTTP_409_CONFLICT, "cycle_test_not_running", "No cycle test is running.")
	return CycleTestSnapshot.model_validate_json(manager.serialize())
