"""Prometheus metrics middleware for FastAPI."""
import time
from typing import Callable

from fastapi import Request, Response
from prometheus_client import Counter, Histogram, Gauge
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match

REQUEST_COUNT = Counter(
	'http_requests_total',
	'Total HTTP requests',
	['method', 'endpoint', 'status']
)

REQUEST_DURATION = Histogram(
	'http_request_duration_seconds',
	'HTTP request duration in seconds',
	['method', 'endpoint']
)

REQUEST_IN_PROGRESS = Gauge(
	'http_requests_in_progress',
	'HTTP requests currently in progress',
	['method', 'endpoint']
)

RESPONSE_SIZE = Histogram(
	'http_response_size_bytes',
	'HTTP response size in bytes',
	['method', 'endpoint']
)


class PrometheusMiddleware(BaseHTTPMiddleware):
	"""Middleware to collect Prometheus metrics for all requests."""

	async def dispatch(self, request: Request, call_next: Callable) -> Response:
		"""Process request and collect metrics."""
		method = request.method
		endpoint = self._get_endpoint(request)

		REQUEST_IN_PROGRESS.labels(method=method, endpoint=endpoint).inc()

		start_time = time.time()
		try:
			response: Response = await call_next(request)
			status = response.status_code

			REQUEST_COUNT.labels(
				method=method,
				endpoint=endpoint,
				status=status
			).inc()

			duration = time.time() - start_time
			REQUEST_DURATION.labels(
				method=method,
				endpoint=endpoint
			).observe(duration)

			if hasattr(response, 'body'):
				RESPONSE_SIZE.labels(
					method=method,
					endpoint=endpoint
				).observe(len(response.body))

			return response

		except Exception as e:
			REQUEST_COUNT.labels(
				method=method,
				endpoint=endpoint,
				status=500
			).inc()
			raise
		finally:
			REQUEST_IN_PROGRESS.labels(method=method, endpoint=endpoint).dec()

	def _get_endpoint(self, request: Request) -> str:
		"""Extract endpoint path template from request."""
		for route in request.app.routes:
			match, _ = route.matches(request)
			if match == Match.FULL:
				return route.path
		return request.url.path
