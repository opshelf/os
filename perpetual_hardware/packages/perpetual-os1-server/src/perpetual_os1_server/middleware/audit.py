"""Audit logging middleware."""
import json
import time
import traceback
from datetime import datetime
from typing import Optional, Dict, Any, List
from contextvars import ContextVar

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import Message
from sqlalchemy.orm import Session

from ..database.session import AsyncSessionLocal
from ..database.models import AuditLog, User
from ..routers.auth_deps import get_current_user_optional

nats_commands_context: ContextVar[List[Dict[str, Any]]] = ContextVar('nats_commands', default=[])

class AuditMiddleware(BaseHTTPMiddleware):
	"""Middleware to log all API requests and responses."""
	
	async def dispatch(self, request: Request, call_next):
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
			
			if isinstance(response, Response):
				body_bytes = b""
				async for chunk in response.body_iterator:
					body_bytes += chunk
				
				if body_bytes:
					try:
						response_body = json.loads(body_bytes)
					except json.JSONDecodeError:
						response_body = {"content": body_bytes.decode("utf-8", errors="ignore")}
				
				response = Response(
					content=body_bytes,
					status_code=status_code,
					headers=dict(response.headers),
					media_type=response.media_type
				)
			
		except Exception as e:
			error_message = str(e)
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
	):
		"""Log request to database."""
		async with AsyncSessionLocal() as db:
			try:
				# Extract token from request if available
				token = None
				auth_header = request.headers.get("Authorization")
				if auth_header and auth_header.startswith("Bearer "):
					token = auth_header.split(" ")[1]
				
				user = await get_current_user_optional(token=token, db=db)
				
				audit_log = AuditLog(
					user_id=user.id if user else None,
					timestamp=datetime.utcnow(),
					method=request.method,
					endpoint=str(request.url.path),
					request_body=request_body,
					response_status=response_status,
					response_body=response_body,
					nats_commands=nats_commands_context.get(),
					error_message=error_message,
					ip_address=request.client.host if request.client else None,
					user_agent=request.headers.get("user-agent"),
					request_duration_ms=duration_ms
				)
				
				db.add(audit_log)
				await db.commit()
			except Exception:
				traceback.print_exc()

def capture_nats_command(command: Dict[str, Any]):
	"""Capture NATS command for audit logging."""
	try:
		commands = nats_commands_context.get()
		commands.append({
			"timestamp": datetime.utcnow().isoformat(),
			"command": command
		})
		nats_commands_context.set(commands)
	except Exception:
		pass