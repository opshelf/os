"""Audit logging middleware."""
import json
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Callable
from contextvars import ContextVar

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ..database.session import AsyncSessionLocal
from ..database.models import AuditLog
from sqlalchemy import delete, func, select
from ..utils.audit_integrity import (
    canonicalize_payload,
    sha256_hex,
    hmac_sha256_hex,
    build_binding_message,
    get_hmac_key_id,
)
from ..utils.audit_constants import AUDIT_RETENTION_DAYS, LEGACY_COMMAND_RECORDS_FIELD
from ..utils.cors_policy import response_allow_origin, resolve_allowed_origins
from ..routers.auth_deps import get_current_user_optional

command_records_context: ContextVar[List[Dict[str, Any]]] = ContextVar("command_records", default=[])

IGNORED_METHODS = frozenset(["OPTIONS"])
ALWAYS_EXCLUDED_ENDPOINTS = frozenset([
    "/api/health",
    "/api/robot/debug/motors/probe",
    "/api/calibration/laser/stream",
])
SUCCESSFUL_READ_METHODS = frozenset(["GET", "HEAD"])
# Reads that had to move to POST so the barcode can travel in the body; they stay unaudited
# like their GET counterparts, and only failures are recorded.
SUCCESSFUL_READ_POST_ENDPOINTS = frozenset([
    "/api/storage/items/lookup",
])
SUCCESSFUL_READ_EXCLUDED_ENDPOINTS = frozenset([
    "/api/auth/me",
    "/api/auth/users",
    "/api/access_levels",
    "/api/access-levels",
    "/api/storage/items",
    "/api/storage/grid",
    "/api/storage/locations",
    "/api/system/info",
    "/api/system/mode",
    "/api/telemetry",
    "/api/camera",
    "/api/robot/status",
    "/api/robot/events",
    "/api/robot/debug/scale",
    "/api/robot/debug/scale/calibration/status",
    "/api/robot/debug/scale/calibration/params",
    "/api/calibration/status",
    "/api/calibration/laser/status",
])
SUCCESSFUL_READ_EXCLUDED_PREFIXES = (
    "/api/storage/items/",
    "/api/robot/debug/",
)
SENSITIVE_FIELD_NAMES = frozenset([
    "pin",
    "password",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "secret",
    "api_key",
    "hmac_key",
])
REDACTED_VALUE = "[REDACTED]"


def is_successful_read_excluded_endpoint(path: str) -> bool:
    if path in SUCCESSFUL_READ_EXCLUDED_ENDPOINTS:
        return True
    return any(path.startswith(prefix) for prefix in SUCCESSFUL_READ_EXCLUDED_PREFIXES)


def should_skip_before_request(request: Request) -> bool:
    if request.method in IGNORED_METHODS:
        return True
    return request.url.path in ALWAYS_EXCLUDED_ENDPOINTS


def should_skip_after_response(request: Request, status_code: int) -> bool:
    if status_code >= 400:
        return False
    if request.url.path in SUCCESSFUL_READ_POST_ENDPOINTS:
        return True
    if request.method not in SUCCESSFUL_READ_METHODS:
        return False
    return is_successful_read_excluded_endpoint(request.url.path)


def redact_sensitive_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED_VALUE if key.lower() in SENSITIVE_FIELD_NAMES else redact_sensitive_values(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_values(item) for item in value]
    return value

class AuditMiddleware(BaseHTTPMiddleware):
	"""Middleware to log all API requests and responses."""

	async def dispatch(self, request: Request, call_next: Callable) -> Response:
		"""Process and log each request."""
		if should_skip_before_request(request):
			return await call_next(request)

		command_records_context.set([])
		
		start_time = time.time()
		
		request_body = None
		if request.method in ["POST", "PUT", "PATCH", "DELETE"]:
			try:
				body = await request.body()
				if body:
					request_body = redact_sensitive_values(json.loads(body))
				
				async def receive() -> dict[str, Any]:
					return {"type": "http.request", "body": body}
				
				request._receive = receive
			except Exception:
				pass
		
		response_body = None
		status_code = 500
		error_message = None
		
		try:
			response: Response = await call_next(request)
			status_code = response.status_code

		except Exception as e:
			error_message = str(e)
			traceback.print_exc()
			response = JSONResponse(
				status_code=500,
				content={"detail": {"message": "Internal server error", "code": "internal_error"}}
			)
			# This response never travels back through the CORS middleware, so it carries
			# the origin header itself, under the same policy as every other response.
			allow_origin = response_allow_origin(
				resolve_allowed_origins(), request.headers.get("origin")
			)
			if allow_origin is not None:
				response.headers["Access-Control-Allow-Origin"] = allow_origin

		duration_ms = int((time.time() - start_time) * 1000)

		if should_skip_after_response(request, status_code):
			return response

		try:
			await self._log_request(
				request=request,
				request_body=request_body,
				response_status=status_code,
				response_body=response_body,
				error_message=error_message or "",
				duration_ms=duration_ms
			)
		except Exception:
			traceback.print_exc()

		return response
	
	async def _log_request(
		self,
		request: Request,
		request_body: Optional[Dict[str, Any]],
		response_status: int,
		response_body: Optional[Dict[str, Any]],
		error_message: Optional[str],
		duration_ms: int
	) -> None:
		"""Log request to database."""
		async with AsyncSessionLocal() as db:
			try:
				token = None
				auth_header = request.headers.get("Authorization")
				if auth_header and auth_header.startswith("Bearer "):
					token = auth_header.split(" ")[1]
				
				user = await get_current_user_optional(token=token, db=db)

				now = datetime.now(timezone.utc)
				user_id = user.id if user else None
				method = request.method
				endpoint = str(request.url.path)
				command_records = command_records_context.get()
				ip_address = request.client.host if request.client else None
				user_agent = request.headers.get("user-agent")
				correlation_id = request.headers.get("X-Correlation-ID")

				last_seq_no = None
				last_entry_hash = None
				result = await db.execute(
					select(AuditLog.seq_no, AuditLog.entry_hash, AuditLog.id)
					.where(AuditLog.entry_hash.isnot(None))
					.order_by(AuditLog.seq_no.desc(), AuditLog.id.desc())
					.limit(1)
				)
				row = result.first()
				if row is not None and row[0] is not None and row[1] is not None:
					last_seq_no = int(row[0])
					last_entry_hash = str(row[1])
				else:
					max_seq = await db.scalar(select(func.max(AuditLog.seq_no)))
					last_seq_no = int(max_seq) if max_seq is not None else 0
					last_entry_hash = None

				seq_no = (last_seq_no or 0) + 1
				prev_hash = last_entry_hash if last_entry_hash else ("0" * 64)

				payload = {
					"timestamp": now.isoformat(),
					"method": method,
					"endpoint": endpoint,
					"request_body": request_body,
					"response_status": response_status,
					"response_body": response_body,
					LEGACY_COMMAND_RECORDS_FIELD: command_records,
					"error_message": error_message,
					"ip_address": ip_address,
					"user_agent": user_agent,
					"request_duration_ms": duration_ms,
					"user_id": user_id,
					"seq_no": seq_no,
					"prev_hash": prev_hash,
				}
				if correlation_id is not None:
					payload["correlation_id"] = correlation_id
				payload_bytes = canonicalize_payload(payload)
				entry_hash = sha256_hex(payload_bytes)
				binding = build_binding_message(seq_no, prev_hash, entry_hash)
				hmac_hex = hmac_sha256_hex(binding)
				key_id = get_hmac_key_id()

				audit_log = AuditLog(
					user_id=user_id,  # type: ignore[arg-type]
					timestamp=now,
					method=method,
					endpoint=endpoint,
					request_body=request_body,
					response_status=response_status,
					response_body=response_body,
					command_records=command_records,
					error_message=error_message or "",
					ip_address=ip_address or "",
					user_agent=user_agent or "",
					request_duration_ms=duration_ms,
					correlation_id=correlation_id,
					seq_no=seq_no,
					prev_hash=prev_hash,
					entry_hash=entry_hash,
					hmac=hmac_hex,
					hmac_key_id=key_id,
				)

				cutoff = now - timedelta(days=AUDIT_RETENTION_DAYS)
				oldest_kept_seq = await db.scalar(
					select(func.min(AuditLog.seq_no)).where(
						AuditLog.seq_no.isnot(None),
						AuditLog.timestamp >= cutoff,
					)
				)
				prune_boundary = int(oldest_kept_seq) if oldest_kept_seq is not None else seq_no
				await db.execute(
					delete(AuditLog).where(
						AuditLog.seq_no.isnot(None),
						AuditLog.seq_no < prune_boundary,
					)
				)
				await db.execute(
					delete(AuditLog).where(
						AuditLog.seq_no.is_(None),
						AuditLog.timestamp < cutoff,
					)
				)

				db.add(audit_log)
				await db.commit()
			except Exception:
				traceback.print_exc()
