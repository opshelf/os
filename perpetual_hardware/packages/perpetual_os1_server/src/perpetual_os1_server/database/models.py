"""Database models for authentication and audit system."""
from typing import List, Dict, Any, Optional
from sqlalchemy import Integer, String, Boolean, DateTime, Text, JSON, ForeignKey, func, Index, Float, UniqueConstraint
from sqlalchemy.orm import relationship, Mapped, mapped_column
from datetime import datetime, timezone
from .base import Base
from pydantic import ConfigDict
from ..utils.audit_constants import LEGACY_COMMAND_RECORDS_FIELD

class Role(Base):
	"""Role model for RBAC."""
	__tablename__ = "roles"

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
	permissions: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
	created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
	updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

	users: Mapped[List["User"]] = relationship("User", back_populates="role")


class AccessLevel(Base):
	"""Access level model for tier-based access control."""
	__tablename__ = "access_levels"

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
	updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

	users: Mapped[List["User"]] = relationship("User", back_populates="access_level")
	items: Mapped[List["Item"]] = relationship("Item", back_populates="access_level")


class User(Base):
	"""User model for authentication."""
	__tablename__ = "users"

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	email: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
	first_name: Mapped[str] = mapped_column(String(50), nullable=False)
	last_name: Mapped[str] = mapped_column(String(50), nullable=False)
	pin_hash: Mapped[str] = mapped_column(String(255), nullable=False)
	role_id: Mapped[int] = mapped_column(Integer, ForeignKey("roles.id"), nullable=False)
	access_level_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("access_levels.id"), nullable=True)
	is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
	is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
	updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

	role: Mapped["Role"] = relationship("Role", back_populates="users")
	access_level: Mapped[Optional["AccessLevel"]] = relationship("AccessLevel", back_populates="users")
	audit_logs: Mapped[List["AuditLog"]] = relationship("AuditLog", back_populates="user")

class AuditLog(Base):
	"""Audit log model for tracking all API operations."""
	__tablename__ = "audit_logs"

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
	timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
	method: Mapped[str] = mapped_column(String(10), nullable=False)
	endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
	request_body: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
	response_status: Mapped[int] = mapped_column(Integer, nullable=False)
	response_body: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
	command_records: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(LEGACY_COMMAND_RECORDS_FIELD, JSON, nullable=True)
	error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
	ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
	user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
	request_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

	seq_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
	prev_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
	entry_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
	hmac: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
	hmac_key_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

	user: Mapped[Optional["User"]] = relationship("User", back_populates="audit_logs")

class Item(Base):
	"""Item model for storing inventory items."""
	__tablename__ = "items"

	model_config = ConfigDict(arbitrary_types_allowed=True)

	def __init__(self, **kwargs: Any) -> None:
		super().__init__(**kwargs)

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	upca: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
	name: Mapped[str] = mapped_column(String(255), nullable=False)
	dimensions: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)
	weight: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
	item_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True, default=dict)
	access_level_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("access_levels.id"), nullable=True)
	created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
	updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

	locations: Mapped[List["ItemLocation"]] = relationship(
		"ItemLocation",
		back_populates="item",
		order_by="ItemLocation.updated_at.desc()"
	)
	access_level: Mapped[Optional["AccessLevel"]] = relationship("AccessLevel", back_populates="items")

class ItemLocation(Base):
	"""ItemLocation model for tracking where items are stored."""
	__tablename__ = "item_locations"

	model_config = ConfigDict(arbitrary_types_allowed=True)

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	item_id: Mapped[int] = mapped_column(Integer, ForeignKey("items.id"), nullable=False)
	side: Mapped[str] = mapped_column(String(20), nullable=False, default="left_module")
	shelf_idx: Mapped[int] = mapped_column(Integer, nullable=False)
	cell_idx: Mapped[int] = mapped_column(Integer, nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
	updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

	item: Mapped["Item"] = relationship("Item", back_populates="locations")

	__table_args__ = (
		UniqueConstraint('side', 'shelf_idx', 'cell_idx', name='uq_item_locations_cell'),
		Index('idx_item_locations_item_id', 'item_id'),
	)

class Telemetry(Base):
	__tablename__ = "telemetry"

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	cycle_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
	motor_movement_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
	updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class StorageCell(Base):
	"""StorageCell model for defining available storage locations."""
	__tablename__ = "storage_cells"

	model_config = ConfigDict(arbitrary_types_allowed=True)

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	shelf_idx: Mapped[int] = mapped_column(Integer, nullable=False)
	cell_idx: Mapped[int] = mapped_column(Integer, nullable=False)
	capacity: Mapped[str] = mapped_column(String(50), nullable=False)
	status: Mapped[str] = mapped_column(String(50), nullable=False, default="available")
	created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
	updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

	__table_args__ = (
		Index('idx_storage_cells_shelf_cell', 'shelf_idx', 'cell_idx', unique=True),
	)
