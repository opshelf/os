"""Audit logging middleware."""
import json
import time
import traceback
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from contextvars import ContextVar

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import Message
from sqlalchemy.orm import Session

from ..database.session import AsyncSessionLocal
from ..database.models import AuditLog, User
from sqlalchemy import select
from ..utils.audit_integrity import (
    canonicalize_payload,
    sha256_hex,
    hmac_sha256_hex,
    build_binding_message,
    get_hmac_key_id,
)
from ..routers.auth_deps import get_current_user_optional

nats_commands_context: ContextVar[List[Dict[str, Any]]] = ContextVar('nats_commands', default=[])

class AuditMiddleware(BaseHTTPMiddleware):
	"""Middleware to log all API requests and responses."""
	
	async def dispatch(self, request: Request, call_next) -> Response:
		"""Process and log each request."""
		nats_commands_context.set([])
		
		start_time = time.time()
		
		request_body = None
		if request.method in ["POST", "PUT", "PATCH", "DELETE"]:
			try:
				body = await request.body()
				if body:
					request_body = json.loads(body)
				
				async def receive():
					return {"type": "http.request", "body": body}
				
				request._receive = receive
			except Exception:
				pass
		
		response_body = None
		status_code = 500
		error_message = None
		
		try:
			response = await call_next(request)
			status_code = response.status_code
			
			# We avoid consuming/rewrapping the response body to prevent side effects.
			# Only capture status_code; leave response as-is.
			
		except Exception as e:
			error_message = str(e)
			traceback.print_exc()
			response = JSONResponse(
				status_code=500,
				content={"detail": "Internal server error"}
			)
		
		duration_ms = int((time.time() - start_time) * 1000)
		
		try:
			await self._log_request(
				request=request,
				request_body=request_body,
				response_status=status_code,
				response_body=response_body,
				error_message=error_message,
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
				# Extract token from request if available
				token = None
				auth_header = request.headers.get("Authorization")
				if auth_header and auth_header.startswith("Bearer "):
					token = auth_header.split(" ")[1]
				
				user = await get_current_user_optional(token=token, db=db)

				# Prepare immutable fields for integrity protection
				now = datetime.now(timezone.utc)
				user_id = user.id if user else None
				method = request.method
				endpoint = str(request.url.path)
				nats_commands = nats_commands_context.get()
				ip_address = request.client.host if request.client else None
				user_agent = request.headers.get("user-agent")

				# Determine previous link (last protected entry), fallback to last id
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
					# Fallback to last id as baseline
					res2 = await db.execute(
						select(AuditLog.id).order_by(AuditLog.id.desc()).limit(1)
					)
					row2 = res2.first()
					last_seq_no = int(row2[0]) if row2 is not None else 0
					last_entry_hash = None

				seq_no = (last_seq_no or 0) + 1
				prev_hash = last_entry_hash if last_entry_hash else ("0" * 64)

				# Canonical payload for hashing
				payload = {
					"timestamp": now.isoformat(),
					"method": method,
					"endpoint": endpoint,
					"request_body": request_body,
					"response_status": response_status,
					"response_body": response_body,
					"nats_commands": nats_commands,
					"error_message": error_message,
					"ip_address": ip_address,
					"user_agent": user_agent,
					"request_duration_ms": duration_ms,
					"user_id": user_id,
					"seq_no": seq_no,
					"prev_hash": prev_hash,
				}
				payload_bytes = canonicalize_payload(payload)
				entry_hash = sha256_hex(payload_bytes)
				binding = build_binding_message(seq_no, prev_hash, entry_hash)
				hmac_hex = hmac_sha256_hex(binding)
				key_id = get_hmac_key_id()

				audit_log = AuditLog(
					user_id=user_id,
					timestamp=now,
					method=method,
					endpoint=endpoint,
					request_body=request_body,
					response_status=response_status,
					response_body=response_body,
					nats_commands=nats_commands,
					error_message=error_message,
					ip_address=ip_address,
					user_agent=user_agent,
					request_duration_ms=duration_ms,
					seq_no=seq_no,
					prev_hash=prev_hash,
					entry_hash=entry_hash,
					hmac=hmac_hex,
					hmac_key_id=key_id,
				)

				db.add(audit_log)
				await db.commit()
			except Exception:
				traceback.print_exc()

def capture_nats_command(command: Dict[str, Any]) -> None:
	"""Capture NATS command for audit logging."""
	try:
		commands = nats_commands_context.get()
		commands.append({
			"timestamp": datetime.now(timezone.utc).isoformat(),
			"command": command
		})
		nats_commands_context.set(commands)
	except Exception:
		pass
