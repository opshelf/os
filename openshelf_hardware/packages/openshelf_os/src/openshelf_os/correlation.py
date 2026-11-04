"""Correlation id of the request a log line belongs to, shared by the server and the robot worker."""
from contextvars import ContextVar

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
