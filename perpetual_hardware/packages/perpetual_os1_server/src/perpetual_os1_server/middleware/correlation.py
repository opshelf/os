"""Correlation ID middleware for request tracing."""
import uuid
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from ..core.logging_config import set_correlation_id, clear_context


class CorrelationIdMiddleware(BaseHTTPMiddleware):
	"""Middleware to add correlation IDs to all requests."""

	async def dispatch(self, request: Request, call_next: Callable) -> Response:
		"""Process request and add correlation ID."""
		correlation_id = request.headers.get('X-Correlation-ID')

		if correlation_id is None:
			correlation_id = str(uuid.uuid4())

		set_correlation_id(correlation_id)

		try:
			response: Response = await call_next(request)
			response.headers['X-Correlation-ID'] = correlation_id
			return response
		finally:
			clear_context()
