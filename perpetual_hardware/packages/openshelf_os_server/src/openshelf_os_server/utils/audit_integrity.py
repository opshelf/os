"""Lightweight audit integrity helpers (SQLite-friendly).

Provides canonical payload serialization, SHA-256 hashing, and HMAC signing
for audit log entries, with a pragmatic env-based key.
"""
import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, Optional

from openshelf_os.common import OperatingMode
from openshelf_os_server.settings import get_operating_mode


_HMAC_KEY_CACHE: Optional[bytes] = None
_HMAC_KEY_ID: str = os.getenv("AUDIT_HMAC_KEY_ID", "v1")
logger = logging.getLogger(__name__)


def _load_hmac_key() -> bytes:
    global _HMAC_KEY_CACHE
    if _HMAC_KEY_CACHE is not None:
        return _HMAC_KEY_CACHE

    key = os.getenv("AUDIT_HMAC_KEY")
    if key:
        try:
            _HMAC_KEY_CACHE = bytes.fromhex(key)
        except ValueError:
            _HMAC_KEY_CACHE = key.encode("utf-8")
    elif get_operating_mode() == OperatingMode.production:
        raise RuntimeError("AUDIT_HMAC_KEY must be set in production")
    else:
        _HMAC_KEY_CACHE = b"development-audit-hmac-key-not-for-production"
        logger.warning("AUDIT_HMAC_KEY is not set; using deterministic development key")

    return _HMAC_KEY_CACHE


def get_hmac_key_id() -> str:
    return _HMAC_KEY_ID


def canonicalize_payload(payload: Dict[str, Any]) -> bytes:
    """Serialize payload deterministically with sorted keys, no spaces.

    All values should be JSON-serializable. None is allowed.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_sha256_hex(message: bytes) -> str:
    key = _load_hmac_key()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def build_binding_message(seq_no: int, prev_hash: str, entry_hash: str) -> bytes:
    return f"{seq_no}|{prev_hash}|{entry_hash}".encode("utf-8")
