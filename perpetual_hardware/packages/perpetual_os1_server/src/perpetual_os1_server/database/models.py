"""Database models for authentication and audit system."""
from typing import List, Dict, Any, Optional
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, JSON, ForeignKey, func, Index, Float
from sqlalchemy.orm import relationship, Mapped, mapped_column
from datetime import datetime, timezone
from .base import Base
from pydantic import ConfigDict

class Role(Base):
	"""Role model for RBAC."""
	__tablename__ = "roles"

	id: Mapped[int] = Column(Integer, primary_key=True)
	name: Mapped[str] = Column(String(50), unique=True, nullable=False)
	permissions: Mapped[List[str]] = Column(JSON, nullable=False, default=list)
	created_at: Mapped[datetime] = Column(DateTime, server_default=func.now())
	updated_at: Mapped[datetime] = Column(DateTime, server_default=func.now(), onupdate=func.now())

	users: Mapped[List["User"]] = relationship("User", back_populates="role")

class AccessLevel(Base):
	"""Access level model for tier-based access control."""
	__tablename__ = "access_levels"

	id: Mapped[int] = Column(Integer, primary_key=True)
	name: Mapped[str] = Column(String(50), unique=True, nullable=False)
	created_at: Mapped[datetime] = Column(DateTime, server_default=func.now())
	updated_at: Mapped[datetime] = Column(DateTime, server_default=func.now(), onupdate=func.now())

	users: Mapped[List["User"]] = relationship("User", back_populates="access_level")

class User(Base):
	"""User model for authentication."""
	__tablename__ = "users"

	id: Mapped[int] = Column(Integer, primary_key=True)
	email: Mapped[str] = Column(String(100), unique=True, nullable=False)
	first_name: Mapped[str] = Column(String(50), nullable=False)
	last_name: Mapped[str] = Column(String(50), nullable=False)
	pin_hash: Mapped[str] = Column(String(255), nullable=False)
	role_id: Mapped[int] = Column(Integer, ForeignKey("roles.id"), nullable=False)
	access_level_id: Mapped[int] = Column(Integer, ForeignKey("access_levels.id"), nullable=True)  # type: ignore[assignment]
	is_active: Mapped[bool] = Column(Boolean, default=True, nullable=False)
	created_at: Mapped[datetime] = Column(DateTime, server_default=func.now())
	updated_at: Mapped[datetime] = Column(DateTime, server_default=func.now(), onupdate=func.now())

	role: Mapped["Role"] = relationship("Role", back_populates="users")
	access_level: Mapped[Optional["AccessLevel"]] = relationship("AccessLevel", back_populates="users")
	audit_logs: Mapped[List["AuditLog"]] = relationship("AuditLog", back_populates="user")

class AuditLog(Base):
    """Audit log model for tracking all API operations."""
    __tablename__ = "audit_logs"

    id: Mapped[int] = Column(Integer, primary_key=True)
    user_id: Mapped[int] = Column(Integer, ForeignKey("users.id"), nullable=True)
    timestamp: Mapped[datetime] = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    method: Mapped[str] = Column(String(10), nullable=False)
    endpoint: Mapped[str] = Column(String(255), nullable=False)
    request_body: Mapped[Optional[Dict[str, Any]]] = Column(JSON, nullable=True)
    response_status: Mapped[int] = Column(Integer, nullable=False)
    response_body: Mapped[Optional[Dict[str, Any]]] = Column(JSON, nullable=True)
    nats_commands: Mapped[Optional[List[Dict[str, Any]]]] = Column(JSON, nullable=True)
    error_message: Mapped[str] = Column(Text, nullable=True)
    ip_address: Mapped[str] = Column(String(45), nullable=True)
    user_agent: Mapped[str] = Column(String(255), nullable=True)
    request_duration_ms: Mapped[int] = Column(Integer, nullable=True)

    seq_no: Mapped[int] = Column(Integer, nullable=True, index=True)
    prev_hash: Mapped[str] = Column(String(64), nullable=True)
    entry_hash: Mapped[str] = Column(String(64), nullable=True)
    hmac: Mapped[str] = Column(String(64), nullable=True)
    hmac_key_id: Mapped[str] = Column(String(50), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="audit_logs")

class Item(Base):
    """Item model for storing inventory items."""
    __tablename__ = "items"

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    id: Mapped[int] = Column(Integer, primary_key=True)
    upca: Mapped[str] = Column(String(255), unique=True, nullable=False, index=True)
    name: Mapped[str] = Column(String(255), nullable=False)
    dimensions: Mapped[Dict[str, Any]] = Column(JSON, nullable=False)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    item_metadata: Mapped[Optional[Dict[str, Any]]] = Column(JSON, nullable=True, default=dict)
    created_at: Mapped[datetime] = Column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = Column(DateTime, server_default=func.now(), onupdate=func.now())

    locations: Mapped[List["ItemLocation"]] = relationship("ItemLocation", back_populates="item")

class ItemLocation(Base):
    """ItemLocation model for tracking where items are stored."""
    __tablename__ = "item_locations"

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: Mapped[int] = Column(Integer, primary_key=True)
    item_id: Mapped[int] = Column(Integer, ForeignKey("items.id"), nullable=False)
    shelf_idx: Mapped[int] = Column(Integer, nullable=False)
    cell_idx: Mapped[int] = Column(Integer, nullable=False)
    created_at: Mapped[datetime] = Column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_item_locations_shelf_cell', 'shelf_idx', 'cell_idx'),
    )

    item: Mapped["Item"] = relationship("Item", back_populates="locations")

    __table_args__ = (
        Index('idx_item_locations_item_id', 'item_id'),
    )

class StorageCell(Base):
    """StorageCell model for defining available storage locations."""
    __tablename__ = "storage_cells"

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: Mapped[int] = Column(Integer, primary_key=True)
    shelf_idx: Mapped[int] = Column(Integer, nullable=False)
    cell_idx: Mapped[int] = Column(Integer, nullable=False)
    capacity: Mapped[str] = Column(String(50), nullable=False)
    status: Mapped[str] = Column(String(50), nullable=False, default="available")
    created_at: Mapped[datetime] = Column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_storage_cells_shelf_cell', 'shelf_idx', 'cell_idx', unique=True),
    )
