"""Authentication core functionality."""
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, cast
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
import jwt
from jwt.exceptions import InvalidTokenError

ph = PasswordHasher()

class AuthService:
	"""Authentication service for managing tokens and passwords."""
	
	def __init__(self, secret_key: str, algorithm: str = "HS256"):
		self.secret_key = secret_key
		self.algorithm = algorithm
	
	def hash_pin(self, pin: str) -> str:
		"""Hash a PIN using Argon2."""
		return ph.hash(pin)
	
	def verify_pin(self, pin: str, pin_hash: str) -> bool:
		"""Verify a PIN against its hash."""
		try:
			ph.verify(pin_hash, pin)
			return True
		except VerifyMismatchError:
			return False
	
	def create_access_token(
		self,
		user_id: int,
		email: str,
		role: str,
		permissions: list[str],
		access_level: Optional[str] = None,
		expires_minutes: int = 30
	) -> str:
		"""Create a JWT access token."""
		expire = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
		data = {
			"sub": email,
			"exp": expire,
			"type": "access",
			"user_id": user_id,
			"email": email,
			"role": role,
			"permissions": permissions,
			"access_level": access_level
		}
		return jwt.encode(data, self.secret_key, algorithm=self.algorithm)
	
	def create_refresh_token(
		self,
		user_id: int,
		email: str,
		expires_days: int = 7
	) -> str:
		"""Create a JWT refresh token."""
		expire = datetime.now(timezone.utc) + timedelta(days=expires_days)
		data = {
			"sub": email,
			"exp": expire,
			"type": "refresh",
			"user_id": user_id,
			"email": email,
			"jti": secrets.token_urlsafe(32)
		}
		return jwt.encode(data, self.secret_key, algorithm=self.algorithm)
	
	def decode_token(self, token: str) -> Optional[Dict[str, Any]]:
		"""Decode and validate a JWT token."""
		try:
			payload = jwt.decode(
				token,
				self.secret_key,
				algorithms=[self.algorithm]
			)
			return cast(Dict[str, Any], payload)
		except InvalidTokenError:
			return None
