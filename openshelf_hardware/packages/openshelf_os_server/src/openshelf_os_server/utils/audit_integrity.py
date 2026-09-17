"""Lightweight audit integrity helpers (SQLite-friendly).

Provides canonical payload serialization, SHA-256 hashing, and HMAC signing
for audit log entries, with a pragmatic env-based key.
"""
import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, List, Optional

from openshelf_os.common import INSECURE_PUBLISHED_SECRETS
from openshelf_os.operating_mode import has_production_traits
from openshelf_os_server.settings import get_operating_mode, get_settings


_HMAC_KEY_CACHE: Optional[bytes] = None
_HMAC_KEY_ID: Optional[str] = None
logger = logging.getLogger(__name__)

# docker-compose.yml published these as signing keys, so anyone who can read the
# repository could forge rows signed with them, and no stack may sign new records
# with them. Records they did sign stay verifiable through this ring, so rotating
# to a real key is not reported as tampering.
PUBLISHED_HMAC_KEYS: Dict[str, List[bytes]] = {
    "v1": [
        b"INSECURE_AUDIT_HMAC_KEY",
        b"development-audit-hmac-key-not-for-production",
    ],
}

_DEV_FALLBACK_KEY = b"development-audit-hmac-key-not-for-production"


def _key_bytes(raw: str) -> bytes:
    # Keys are used as given rather than stretched, because stretching would change
    # the key of every existing deployment and invalidate its stored chain.
    try:
        return bytes.fromhex(raw)
    except ValueError:
        return raw.encode("utf-8")


def _is_published_key(raw: Optional[str]) -> bool:
    return raw is not None and raw in INSECURE_PUBLISHED_SECRETS


def _load_hmac_key() -> bytes:
    global _HMAC_KEY_CACHE
    if _HMAC_KEY_CACHE is not None:
        return _HMAC_KEY_CACHE

    env_key = os.getenv("AUDIT_HMAC_KEY")
    if _is_published_key(env_key):
        logger.warning("AUDIT_HMAC_KEY is a published default, ignoring it for signing")
        env_key = None

    configured_key = env_key or get_settings().auth.audit_hmac_key
    if _is_published_key(configured_key):
        logger.warning("auth.audit_hmac_key is a published default, ignoring it for signing")
        configured_key = None

    if configured_key:
        _HMAC_KEY_CACHE = _key_bytes(configured_key)
    elif has_production_traits(get_operating_mode()) is True:
        raise RuntimeError(
            "AUDIT_HMAC_KEY must be set in demo, mock, and production modes "
            "(./scripts/start generates one per mode)"
        )
    else:
        _HMAC_KEY_CACHE = _DEV_FALLBACK_KEY
        logger.warning("AUDIT_HMAC_KEY is not set; using deterministic development key")

    return _HMAC_KEY_CACHE


def get_hmac_key_id() -> str:
    global _HMAC_KEY_ID
    if _HMAC_KEY_ID is None:
        _HMAC_KEY_ID = os.getenv("AUDIT_HMAC_KEY_ID") or "v1"
    return _HMAC_KEY_ID


def canonicalize_payload(payload: Dict[str, Any]) -> bytes:
    """Serialize payload deterministically with sorted keys, no spaces.

    All values should be JSON-serializable. None is allowed.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_sha256_hex(message: bytes, key: Optional[bytes] = None) -> str:
    signing_key = _load_hmac_key() if key is None else key
    return hmac.new(signing_key, message, hashlib.sha256).hexdigest()


def build_binding_message(seq_no: int, prev_hash: str, entry_hash: str) -> bytes:
    return f"{seq_no}|{prev_hash}|{entry_hash}".encode("utf-8")


def _mac_matches(stored_mac: Optional[str], expected_mac: str) -> bool:
    if stored_mac is None or stored_mac.isascii() is False:
        return False
    return hmac.compare_digest(stored_mac, expected_mac)


def verify_binding_hmac(
    stored_mac: Optional[str],
    message: bytes,
    key_id: Optional[str],
) -> Optional[str]:
    """Return None when a stored binding HMAC verifies, otherwise the failure reason.

    Rows are checked against the key their own `hmac_key_id` names, so a rotated
    key does not make older rows look tampered with.
    """
    if key_id is None or key_id == get_hmac_key_id():
        # Rows without a key id predate the ring and carry the key in use at the time.
        if _mac_matches(stored_mac, hmac_sha256_hex(message)):
            return None
        if key_id is not None:
            # A pre-rotation deployment stamped the current key id on rows it signed
            # with a published key, so fall back to the ring for that id.
            for legacy_key in PUBLISHED_HMAC_KEYS.get(key_id, []):
                if _mac_matches(stored_mac, hmac_sha256_hex(message, legacy_key)):
                    return None
        return "HMAC mismatch"

    for legacy_key in PUBLISHED_HMAC_KEYS.get(key_id, []):
        if _mac_matches(stored_mac, hmac_sha256_hex(message, legacy_key)):
            return None

    if key_id not in PUBLISHED_HMAC_KEYS:
        return f"HMAC mismatch (no key available for key id {key_id})"

    # The id is provisioned, so a mac matching none of its keys is tampering.
    return "HMAC mismatch"
