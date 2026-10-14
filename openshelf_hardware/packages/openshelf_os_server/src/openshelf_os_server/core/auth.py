"""Authentication core functionality."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional, Dict, Any, cast
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
import jwt
from jwt.exceptions import InvalidTokenError

ph = PasswordHasher()

# RFC 7518 section 3.2 requires an HMAC key at least as long as the hash output,
# so a configured secret (which may be short or operator-chosen) is stretched
# instead of being used verbatim. The salt is constant because derived keys must
# keep verifying across processes and restarts.
_KEY_DERIVATION_SALT = b"openshelf_os:jwt-hmac-key"
_PBKDF2_ITERATIONS = 100_000
_HMAC_KEY_LENGTHS = {"HS256": 32, "HS384": 48, "HS512": 64}

@lru_cache(maxsize=8)
def _signing_key(secret_key: str, algorithm: str) -> str | bytes:
	"""Return a key of RFC 7518 length for the given secret and algorithm."""
	key_length = _HMAC_KEY_LENGTHS.get(algorithm)
	if key_length is None:
		# Asymmetric algorithms are configured as key material, leave them alone.
		return secret_key
	return hashlib.pbkdf2_hmac(
		"sha256",
		secret_key.encode("utf-8"),
		_KEY_DERIVATION_SALT,
		_PBKDF2_ITERATIONS,
		dklen=key_length,
	)

class AuthService:
	"""Authentication service for managing tokens and passwords."""
	
	def __init__(self, secret_key: str, algorithm: str = "HS256"):
		# The settings layer is what guarantees a non-empty, non-published
		# secret: PyJWT on the locked version signs and verifies with an empty
		# or short HMAC key without complaint, so reject it here. The derived
		# key exists so any configured secret reaches an RFC 7518-sized key.
		if not secret_key:
			raise ValueError("AuthService secret_key must not be empty")
		self.secret_key = _signing_key(secret_key, algorithm)
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

	def create_scan_token(self, event_seq: int, expires_seconds: int) -> str:
		"""Create a short-lived token naming one durable scanner event."""
		now = datetime.now(timezone.utc)
		data = {
			"iat": now,
			"exp": now + timedelta(seconds=expires_seconds),
			"type": "login_scan",
			"seq": event_seq,
			"jti": secrets.token_urlsafe(16),
		}
		return jwt.encode(data, self.secret_key, algorithm=self.algorithm)

	def decode_scan_token(self, token: str) -> Optional[int]:
		"""Return the durable event sequence carried by a valid scan token."""
		payload = self.decode_token(token)
		if payload is None or payload.get("type") != "login_scan":
			return None
		seq = payload.get("seq")
		return seq if isinstance(seq, int) and not isinstance(seq, bool) else None
	
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
