"""Database models for authentication and audit system."""
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, JSON, ForeignKey, func, Index
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from .base import Base
from pydantic import ConfigDict

class Role(Base):
	"""Role model for RBAC."""
	__tablename__ = "roles"
	
	id = Column(Integer, primary_key=True)
	name = Column(String(50), unique=True, nullable=False)
	permissions = Column(JSON, nullable=False, default=list)
	created_at = Column(DateTime, server_default=func.now())
	updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
	
	users = relationship("User", back_populates="role")

class User(Base):
	"""User model for authentication."""
	__tablename__ = "users"
	
	id = Column(Integer, primary_key=True)
	email = Column(String(100), unique=True, nullable=False)
	first_name = Column(String(50), nullable=False)
	last_name = Column(String(50), nullable=False)
	pin_hash = Column(String(255), nullable=False)
	role_id = Column(Integer, ForeignKey("roles.id"), nullable=False)
	is_active = Column(Boolean, default=True, nullable=False)
	created_at = Column(DateTime, server_default=func.now())
	updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
	
	role = relationship("Role", back_populates="users")
	audit_logs = relationship("AuditLog", back_populates="user")

class AuditLog(Base):
    """Audit log model for tracking all API operations."""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    timestamp = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    method = Column(String(10), nullable=False)
    endpoint = Column(String(255), nullable=False)
    request_body = Column(JSON, nullable=True)
    response_status = Column(Integer, nullable=False)
    response_body = Column(JSON, nullable=True)
    nats_commands = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(255), nullable=True)
    request_duration_ms = Column(Integer, nullable=True)

    # Lightweight tamper-evidence fields (nullable for backward compatibility)
    seq_no = Column(Integer, nullable=True, index=True)
    prev_hash = Column(String(64), nullable=True)
    entry_hash = Column(String(64), nullable=True)
    hmac = Column(String(64), nullable=True)
    hmac_key_id = Column(String(50), nullable=True)

    user = relationship("User", back_populates="audit_logs")

class Item(Base):
    """Item model for storing inventory items."""
    __tablename__ = "items"

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id = Column(Integer, primary_key=True)
    upca = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    dimensions = Column(JSON, nullable=False)
    weight = Column(String(50), nullable=True)
    item_metadata = Column(JSON, nullable=True, default=dict)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationship with item locations
    locations = relationship("ItemLocation", back_populates="item")

class ItemLocation(Base):
    """ItemLocation model for tracking where items are stored."""
    __tablename__ = "item_locations"

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    shelf_idx = Column(Integer, nullable=False)
    cell_idx = Column(Integer, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Index for frequent queries on shelf and cell combinations
    __table_args__ = (
        Index('idx_item_locations_shelf_cell', 'shelf_idx', 'cell_idx'),
    )

    # Relationship with item
    item = relationship("Item", back_populates="locations")

    # Index for finding all locations of a specific item
    __table_args__ += (
        Index('idx_item_locations_item_id', 'item_id'),
    )

class StorageCell(Base):
    """StorageCell model for defining available storage locations."""
    __tablename__ = "storage_cells"

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id = Column(Integer, primary_key=True)
    shelf_idx = Column(Integer, nullable=False)
    cell_idx = Column(Integer, nullable=False)
    capacity = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False, default="available")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Unique index for (shelf_idx, cell_idx) combination
    __table_args__ = (
        Index('idx_storage_cells_shelf_cell', 'shelf_idx', 'cell_idx', unique=True),
    )
