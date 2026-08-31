from typing import List, Dict, Any, Optional
from sqlalchemy import Integer, String, Boolean, DateTime, Text, JSON, ForeignKey, func, Index, Float, UniqueConstraint, TypeDecorator
from sqlalchemy.orm import relationship, Mapped, mapped_column
from datetime import datetime, timezone
from .base import Base
from pydantic import ConfigDict
from ..utils.audit_constants import LEGACY_COMMAND_RECORDS_FIELD


class UTCDateTime(TypeDecorator[datetime]):
	impl = DateTime(timezone=True)
	cache_ok = True

	def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
		if value is None:
			return None
		if value.tzinfo is None:
			return value.replace(tzinfo=timezone.utc)
		return value.astimezone(timezone.utc)

	def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
		if value is None:
			return None
		if value.tzinfo is None:
			return value.replace(tzinfo=timezone.utc)
		return value.astimezone(timezone.utc)

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
	__table_args__ = (
		Index("ix_audit_logs_timestamp", "timestamp"),
		Index("ix_audit_logs_user_id_timestamp", "user_id", "timestamp"),
		Index("ix_audit_logs_endpoint_timestamp", "endpoint", "timestamp"),
		Index("ix_audit_logs_method_endpoint_timestamp", "method", "endpoint", "timestamp"),
		Index("ix_audit_logs_response_status_timestamp", "response_status", "timestamp"),
	)

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
	timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=lambda: datetime.now(timezone.utc))
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
	correlation_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)

	seq_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
	prev_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
	entry_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
	hmac: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
	hmac_key_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

	user: Mapped[Optional["User"]] = relationship("User", back_populates="audit_logs")


class MockRobotCommand(Base):
	"""Durable command lifecycle used only by the mock robot runtime."""
	__tablename__ = "mock_robot_commands"
	__table_args__ = (
		Index("ix_mock_robot_commands_status_created", "status", "created_at"),
		Index("ix_mock_robot_commands_completed_at", "completed_at"),
	)

	command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
	request_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
	payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)
	status: Mapped[str] = mapped_column(String(20), nullable=False)
	result: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
	correlation_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
	created_at: Mapped[datetime] = mapped_column(
		UTCDateTime(), nullable=False, default=lambda: datetime.now(timezone.utc)
	)
	started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
	completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)

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
	sizer_measurements: Mapped[List["ItemSizerMeasurement"]] = relationship("ItemSizerMeasurement", back_populates="item", passive_deletes=True)
	access_level: Mapped[Optional["AccessLevel"]] = relationship("AccessLevel", back_populates="items")

class ItemSizerMeasurement(Base):
	"""Auto-sizer measurement captured during store requests."""
	__tablename__ = "item_sizer_measurements"
	__table_args__ = (
		Index("ix_item_sizer_measurements_item_id", "item_id"),
		Index("ix_item_sizer_measurements_upca", "upca"),
		Index("ix_item_sizer_measurements_request_correlation_id", "request_correlation_id"),
		Index("ix_item_sizer_measurements_created_at", "created_at"),
	)

	model_config = ConfigDict(arbitrary_types_allowed=True)

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	item_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("items.id", ondelete="SET NULL"), nullable=True)
	upca: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
	item_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
	user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
	request_correlation_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

	manual_dimensions: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
	manual_dimensions_source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
	auto_dimensions: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
	raw_measurement: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
	command_payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

	detected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
	error_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
	error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

	measurement_timestamp: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
	created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=lambda: datetime.now(timezone.utc))

	item: Mapped[Optional["Item"]] = relationship("Item", back_populates="sizer_measurements")


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


class StorageBin(Base):
	"""A configured multi-item bin with a fixed home cell."""
	__tablename__ = "storage_bins"
	__table_args__ = (
		UniqueConstraint("side", "shelf_idx", "cell_idx", name="uq_storage_bins_home_cell"),
		Index("ix_storage_bins_status", "status"),
	)

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	barcode: Mapped[str] = mapped_column(String(12), unique=True, nullable=False, index=True)
	side: Mapped[str] = mapped_column(String(20), nullable=False)
	shelf_idx: Mapped[int] = mapped_column(Integer, nullable=False)
	cell_idx: Mapped[int] = mapped_column(Integer, nullable=False)
	inner_dimensions: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)
	max_item_dimensions: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
	max_items: Mapped[int] = mapped_column(Integer, nullable=False)
	status: Mapped[str] = mapped_column(String(24), nullable=False, default="at_home")
	gross_weight: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
	active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
	created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=lambda: datetime.now(timezone.utc))
	updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class StorageBinItem(Base):
	"""Membership of an inventory item in a storage bin."""
	__tablename__ = "storage_bin_items"
	__table_args__ = (
		UniqueConstraint("item_id", name="uq_storage_bin_items_item"),
		Index("ix_storage_bin_items_bin_id", "bin_id"),
	)

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	bin_id: Mapped[int] = mapped_column(Integer, ForeignKey("storage_bins.id", ondelete="CASCADE"), nullable=False)
	item_id: Mapped[int] = mapped_column(Integer, ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
	created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=lambda: datetime.now(timezone.utc))


class StorageBinWorkflow(Base):
	"""Durable, globally exclusive bin retrieval/store workflow."""
	__tablename__ = "storage_bin_workflows"
	__table_args__ = (
		Index("ix_storage_bin_workflows_state", "state"),
		Index("ix_storage_bin_workflows_created_at", "created_at"),
	)

	id: Mapped[str] = mapped_column(String(36), primary_key=True)
	operation: Mapped[str] = mapped_column(String(12), nullable=False)
	state: Mapped[str] = mapped_column(String(32), nullable=False)
	lock_key: Mapped[Optional[str]] = mapped_column(String(16), unique=True, nullable=True)
	recovery_claim: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
	bin_id: Mapped[int] = mapped_column(Integer, ForeignKey("storage_bins.id"), nullable=False)
	item_id: Mapped[int] = mapped_column(Integer, ForeignKey("items.id"), nullable=False)
	user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
	history: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
	error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
	resolution: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
	created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=lambda: datetime.now(timezone.utc))
	updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
	completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)

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
