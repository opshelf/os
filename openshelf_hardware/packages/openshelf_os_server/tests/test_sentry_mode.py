from __future__ import annotations


import asyncio
import json
from typing import Any

import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from openshelf_os import SIMULATED_MOTOR_FAULT_PREFIX
from openshelf_os.exceptions import MotorError
from openshelf_os.robot.commands import HomeCommand
from openshelf_os.robot.worker import RobotWorker
from openshelf_os_server import main


# The value anyio puts on a CancelledError it delivered on behalf of BaseHTTPMiddleware's
# call_next task, which is how a streaming request is cancelled when its response finishes.


def test_before_send_sentry_event_redacts_headers() -> None:
	event = {
		"request": {
			"headers": {
				"authorization": "secret",
				"x-request-id": "request-id",
			}
		}
	}

	assert main._before_send_sentry_event(event, {}) == event
	assert event["request"]["headers"]["authorization"] == "[Filtered]"
	assert event["request"]["headers"]["x-request-id"] == "request-id"



async def test_injected_motor_fault_is_not_sent_to_sentry_but_a_real_one_is() -> None:
	"""Sentry #7805261565: the debug fault-injection crash report is dropped, while a real
	hardware fault with the same text still reports."""
	fault = MotorError(f"{SIMULATED_MOTOR_FAULT_PREFIX}z_axis")

	class FaultingRobot:
		def simulate_motor_crash(self, _motor_name: str) -> bool:
			return True

		def block_motion_for_safety(self) -> None:
			pass

		def release_motion_safety(self) -> None:
			pass

		async def get_status(self) -> None:
			return None

		async def handle_command(self, _command: object) -> None:
			raise fault

	sent: list[dict[str, Any]] = []

	class CapturingTransport(Transport):
		def capture_envelope(self, envelope: Envelope) -> None:
			event = envelope.get_event()
			if event is not None:
				sent.append(event)

	RobotWorker._instance = None
	worker = RobotWorker(FaultingRobot())  # type: ignore[arg-type]
	events = await worker.subscribe_events()
	# A scoped client: the worker's report tasks inherit it, and no global Sentry state leaks.
	with sentry_sdk.new_scope() as scope:
		scope.set_client(
			sentry_sdk.Client(
				dsn="https://key@sentry.invalid/1",
				transport=CapturingTransport,
				default_integrations=False,
				before_send=main._before_send_sentry_event,
			)
		)
		try:
			assert await worker.simulate_motor_crash("z_axis") is True
			await asyncio.gather(*list(worker._background_tasks))
			assert sent == []

			await worker.enqueue_command(HomeCommand())
			worker._running = True
			task = asyncio.create_task(worker.process_commands())
			while json.loads(await asyncio.wait_for(events.get(), timeout=2)).get("command") != "home":
				pass
			worker._running = False
			task.cancel()
			await asyncio.gather(task, return_exceptions=True)
			await asyncio.gather(*list(worker._background_tasks))
			assert [event["contexts"]["hardware_fault"]["message"] for event in sent] == [str(fault)]
		finally:
			worker._running = False
			RobotWorker._instance = None
