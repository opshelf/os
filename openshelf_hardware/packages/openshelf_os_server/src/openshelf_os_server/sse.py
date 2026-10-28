"""SSE response that ends quietly when the client stops reading.

sse-starlette raises SendTimeoutError when one send blocks past ``send_timeout``
(a sleeping laptop or dead Wi-Fi keeps the socket open but drains nothing). It
subclasses TimeoutError, so the mapped 504 handler would catch it after the stream
started and turn a routine client stall into "response already started" plus an
unhandled-error report. The library has already closed the body iterator by then.
"""
from sse_starlette.sse import EventSourceResponse as _EventSourceResponse, SendTimeoutError
from starlette.types import Receive, Scope, Send

from openshelf_os_server.core.logging_config import get_logger

logger = get_logger(__name__)


class EventSourceResponse(_EventSourceResponse):
	async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
		try:
			await super().__call__(scope, receive, send)
		except SendTimeoutError:
			logger.info("sse_client_stalled", path=scope.get("path"), send_timeout=self.send_timeout)
