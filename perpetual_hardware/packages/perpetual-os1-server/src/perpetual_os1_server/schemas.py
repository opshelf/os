"""Pydantic schemas for validation."""
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime

# User schemas
class UserBase(BaseModel):
	"""Base user model."""
	email: EmailStr
	first_name: str
	last_name: str

class UserCreate(UserBase):
	"""User creation with PIN."""
	pin: str
	role_id: int

class UserUpdate(BaseModel):
	"""User update - all fields optional."""
	email: Optional[EmailStr] = None
	first_name: Optional[str] = None
	last_name: Optional[str] = None
	pin: Optional[str] = None
	role_id: Optional[int] = None
	is_active: Optional[bool] = None

class UserInDB(UserBase):
	"""User model from database."""
	id: int
	role_id: int
	role_name: str
	is_active: bool
	created_at: datetime
	updated_at: datetime
	
	class Config:
		from_attributes = True

class UserResponse(UserBase):
	"""User response with details."""
	id: int
	role: str
	permissions: List[str]
	is_active: bool

# Role schemas
class RoleBase(BaseModel):
	"""Base role model."""
	name: str
	permissions: List[str]

class RoleCreate(RoleBase):
	"""Role creation."""
	pass

class RoleUpdate(BaseModel):
	"""Role update - all fields optional."""
	name: Optional[str] = None
	permissions: Optional[List[str]] = None

class RoleInDB(RoleBase):
	"""Role model from database."""
	id: int
	created_at: datetime
	updated_at: datetime
	
	class Config:
		from_attributes = True

# Auth schemas
class LoginRequest(BaseModel):
	"""Login with email/PIN."""
	email: EmailStr
	pin: str

class TokenResponse(BaseModel):
	"""JWT token response."""
	access_token: str
	refresh_token: str
	token_type: str = "bearer"
	expires_in: int

class RefreshRequest(BaseModel):
	"""Refresh token request."""
	refresh_token: str

# Audit schemas
class AuditLogResponse(BaseModel):
	"""Audit log entry."""
	id: int
	user_id: Optional[int]
	user_email: Optional[str]
	timestamp: datetime
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
	
	class Config:
		from_attributes = True