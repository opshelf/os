"""Audit log query API endpoints."""
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import and_
from pydantic import BaseModel

from ..database.session import get_db
from ..database.models import AuditLog, User
from .auth_deps import get_current_user, require_permissions

router = APIRouter(
	prefix="/api/audit",
	tags=["audit"],
	dependencies=[Depends(require_permissions("audit:read"))]
)

class AuditLogResponse(BaseModel):
	"""Audit log response model."""
	id: int
	user_id: Optional[int]
	user_email: Optional[str]
	timestamp: str
	method: str
	endpoint: str
	request_body: Optional[dict]
	response_status: int
	response_body: Optional[dict]
	nats_commands: Optional[List[dict]]
	error_message: Optional[str]
	ip_address: Optional[str]
	user_agent: Optional[str]
	request_duration_ms: Optional[int]

class AuditLogListResponse(BaseModel):
	"""Audit log list response."""
	logs: List[AuditLogResponse]
	total: int
	offset: int
	limit: int

@router.get("/logs", response_model=AuditLogListResponse)
async def list_audit_logs(
	user_id: Optional[int] = Query(None, description="Filter by user ID"),
	date_from: Optional[datetime] = Query(None, description="Start date (ISO format)"),
	date_to: Optional[datetime] = Query(None, description="End date (ISO format)"),
	endpoint: Optional[str] = Query(None, description="Filter by API endpoint"),
	method: Optional[str] = Query(None, description="Filter by HTTP method"),
	status_code: Optional[int] = Query(None, description="Filter by response status"),
	limit: int = Query(100, ge=1, le=1000, description="Max results"),
	offset: int = Query(0, ge=0, description="Pagination offset"),
	db: Session = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> AuditLogListResponse:
	"""Query audit logs with filters."""
	query = db.query(AuditLog)
	
	filters = []
	
	if user_id is not None:
		filters.append(AuditLog.user_id == user_id)
	
	if date_from is not None:
		filters.append(AuditLog.timestamp >= date_from)
	
	if date_to is not None:
		filters.append(AuditLog.timestamp <= date_to)
	
	if endpoint is not None:
		filters.append(AuditLog.endpoint.like(f"%{endpoint}%"))
	
	if method is not None:
		filters.append(AuditLog.method == method.upper())
	
	if status_code is not None:
		filters.append(AuditLog.response_status == status_code)
	
	if filters:
		query = query.filter(and_(*filters))
	
	total = query.count()
	
	logs = query.order_by(AuditLog.timestamp.desc()).offset(offset).limit(limit).all()
	
	return AuditLogListResponse(
		logs=[
			AuditLogResponse(
				id=log.id,
				user_id=log.user_id,
				user_email=log.user.email if log.user else None,
				timestamp=log.timestamp.isoformat(),
				method=log.method,
				endpoint=log.endpoint,
				request_body=log.request_body,
				response_status=log.response_status,
				response_body=log.response_body,
				nats_commands=log.nats_commands,
				error_message=log.error_message,
				ip_address=log.ip_address,
				user_agent=log.user_agent,
				request_duration_ms=log.request_duration_ms
			)
			for log in logs
		],
		total=total,
		offset=offset,
		limit=limit
	)

@router.get("/logs/{log_id}", response_model=AuditLogResponse)
async def get_audit_log(
	log_id: int,
	db: Session = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> AuditLogResponse:
	"""Get a specific audit log entry."""
	log = db.query(AuditLog).filter_by(id=log_id).first()
	
	if log is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="Audit log not found"
		)
	
	return AuditLogResponse(
		id=log.id,
		user_id=log.user_id,
		user_email=log.user.email if log.user else None,
		timestamp=log.timestamp.isoformat(),
		method=log.method,
		endpoint=log.endpoint,
		request_body=log.request_body,
		response_status=log.response_status,
		response_body=log.response_body,
		nats_commands=log.nats_commands,
		error_message=log.error_message,
		ip_address=log.ip_address,
		user_agent=log.user_agent,
		request_duration_ms=log.request_duration_ms
	)