"""Audit log query API endpoints."""
from datetime import datetime
from typing import Optional, List, Any, cast

from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, select, func
from sqlalchemy.orm import selectinload
from pydantic import BaseModel

from ..database.session import get_db
from ..database.models import AuditLog, User
from .auth_deps import get_current_user, require_permissions
from ..utils.audit_constants import LEGACY_COMMAND_RECORDS_FIELD
from ..utils.audit_integrity import canonicalize_payload, sha256_hex, hmac_sha256_hex, build_binding_message

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
	command_records: Optional[List[dict]]
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


class AuditVerifyResponse(BaseModel):
	ok: bool
	checked: int
	first_error_seq: Optional[int]
	message: Optional[str]

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
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> AuditLogListResponse:
	"""Query audit logs with filters."""
	query = select(AuditLog)
	
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
	
	count_query = select(func.count()).select_from(AuditLog)
	if filters:
		count_query = count_query.filter(and_(*filters))
	
	total_result = await db.execute(count_query)
	total = total_result.scalar()
	
	logs_result = await db.execute(
		query.options(selectinload(AuditLog.user))
		.order_by(AuditLog.timestamp.desc())
		.offset(offset)
		.limit(limit)
	)
	logs = logs_result.scalars().all()
	
	return AuditLogListResponse(
		logs=[
			AuditLogResponse(
				id=int(log.id),
				user_id=int(log.user_id) if log.user_id is not None else None,
				user_email=log.user.email if log.user else None,
				timestamp=log.timestamp.isoformat(),
				method=str(log.method),
				endpoint=str(log.endpoint),
				request_body=cast(dict[str, Any], log.request_body),
				response_status=int(log.response_status),
				response_body=cast(dict[str, Any], log.response_body),
				command_records=cast(list[dict[str, Any]], log.command_records),
				error_message=str(log.error_message) if log.error_message is not None else None,
				ip_address=str(log.ip_address) if log.ip_address is not None else None,
				user_agent=str(log.user_agent) if log.user_agent is not None else None,
				request_duration_ms=int(log.request_duration_ms) if log.request_duration_ms is not None else None
			)
			for log in logs
		],
		total=int(total or 0),
		offset=offset,
		limit=limit
	)

@router.get("/logs/{log_id}", response_model=AuditLogResponse)
async def get_audit_log(
	log_id: int,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> AuditLogResponse:
	"""Get a specific audit log entry."""
	log_result = await db.execute(
		select(AuditLog)
		.options(selectinload(AuditLog.user))
		.filter(AuditLog.id == log_id)
	)
	log = log_result.scalar_one_or_none()
	
	if log is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="Audit log not found"
		)
	
	return AuditLogResponse(
		id=int(log.id),
		user_id=int(log.user_id) if log.user_id is not None else None,
		user_email=log.user.email if log.user else None,
		timestamp=log.timestamp.isoformat(),
		method=str(log.method),
		endpoint=str(log.endpoint),
		request_body=cast(dict[str, Any], log.request_body),
		response_status=int(log.response_status),
		response_body=cast(dict[str, Any], log.response_body),
		command_records=cast(list[dict[str, Any]], log.command_records),
		error_message=str(log.error_message) if log.error_message is not None else None,
		ip_address=str(log.ip_address) if log.ip_address is not None else None,
		user_agent=str(log.user_agent) if log.user_agent is not None else None,
		request_duration_ms=int(log.request_duration_ms) if log.request_duration_ms is not None else None
	)


@router.get("/verify", response_model=AuditVerifyResponse)
async def verify_audit_chain(
	from_seq: Optional[int] = Query(None, ge=1, description="Start seq_no (inclusive)"),
	to_seq: Optional[int] = Query(None, ge=1, description="End seq_no (inclusive)"),
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user),
) -> AuditVerifyResponse:
	"""Verify linkage, hashes, and HMACs over protected audit entries.

	Existing queries remain unchanged; this is an additive, lightweight check.
	"""
	# Build base query over protected entries only
	query = select(AuditLog).where(AuditLog.entry_hash.isnot(None))
	if from_seq is not None:
		query = query.filter(AuditLog.seq_no >= from_seq)
	if to_seq is not None:
		query = query.filter(AuditLog.seq_no <= to_seq)
	query = query.order_by(AuditLog.seq_no.asc())

	result = await db.execute(query)
	logs = result.scalars().all()

	checked = 0
	prev_hash = None
	first_error_seq = None
	error_message = None

	for log in logs:
		# Rebuild payload deterministically to recompute hash
		# SQLite doesn't preserve timezone info, so we need to add it back
		from datetime import timezone as tz
		timestamp_iso = None
		if log.timestamp:
			if log.timestamp.tzinfo is None:
				# Add UTC timezone info since audit logs are created with UTC
				timestamp_iso = log.timestamp.replace(tzinfo=tz.utc).isoformat()
			else:
				timestamp_iso = log.timestamp.isoformat()
		
		payload = {
			"timestamp": timestamp_iso,
			"method": log.method,
			"endpoint": log.endpoint,
			"request_body": log.request_body,
			"response_status": log.response_status,
			"response_body": log.response_body,
			LEGACY_COMMAND_RECORDS_FIELD: log.command_records,
			"error_message": log.error_message,
			"ip_address": log.ip_address,
			"user_agent": log.user_agent,
			"request_duration_ms": log.request_duration_ms,
			"user_id": log.user_id,
			"seq_no": log.seq_no,
			"prev_hash": log.prev_hash,
		}
		payload_bytes = canonicalize_payload(payload)
		recomputed_hash = sha256_hex(payload_bytes)

		# Check entry hash matches
		if log.entry_hash != recomputed_hash:
			first_error_seq = log.seq_no
			error_message = "Entry hash mismatch"
			break

		# Check linkage with previous
		expected_prev = prev_hash if prev_hash is not None else ("0" * 64)
		if log.prev_hash != expected_prev:
			first_error_seq = log.seq_no
			error_message = "Prev hash mismatch (chain break)"
			break

		# Check HMAC over binding
		binding = build_binding_message(int(log.seq_no) if log.seq_no is not None else 0, str(log.prev_hash), str(log.entry_hash))
		expected_hmac = hmac_sha256_hex(binding)
		if log.hmac != expected_hmac:
			first_error_seq = log.seq_no
			error_message = "HMAC mismatch"
			break

		prev_hash = log.entry_hash
		checked += 1

	ok = first_error_seq is None
	return AuditVerifyResponse(
		ok=ok,
		checked=checked,
		first_error_seq=first_error_seq,
		message=error_message,
	)
