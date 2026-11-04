"""Lightweight audit integrity helpers (SQLite-friendly).

Provides canonical payload serialization, SHA-256 hashing, and HMAC signing
for audit log entries. `../provision` delivers one HMAC key per device; a
checkout or image that provisioning has not reached keeps a generated key of its
own per mode, so the chain stays verifiable across restarts either way.
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openshelf_os.common import is_insecure_published_secret
from openshelf_os.operating_mode import OperatingMode
from openshelf_os.runtime_state import DATA_DIR_ENV
from openshelf_os_server.settings import get_operating_mode, get_settings


_HMAC_KEY_CACHE: Optional[bytes] = None
_HMAC_KEY_ID: Optional[str] = None
_RING_KEY_CACHE: Dict[str, List[bytes]] = {}
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

# Provisioning delivers one key per device under this id. Keys generated here carry
# mode-scoped ids that never collide with it: when a device loses its provisioned
# key, the rows signed under it read as "no key available for key id v2" (missing
# key) instead of false tampering, and a development or demo workspace forked from
# production still finds the right key because `_ring_keys` matches ids to the
# per-mode key files.
PROVISIONED_HMAC_KEY_ID = "v2"
GENERATED_KEY_FILE_PREFIX = "audit-hmac-key"
GENERATED_KEY_BYTES = 48


def _key_bytes(raw: str) -> bytes:
    # Keys are used as given rather than stretched, because stretching would change
    # the key of every existing deployment and invalidate its stored chain.
    try:
        return bytes.fromhex(raw)
    except ValueError:
        return raw.encode("utf-8")


def _is_published_key(raw: Optional[str]) -> bool:
    return is_insecure_published_secret(raw)


def _usable_key(raw: Optional[str], source: str) -> Optional[str]:
    """Return the key unless it is unset or a key published in the repository."""
    if raw is None or raw == "":
        return None
    if _is_published_key(raw) is True:
        logger.warning("%s is a published default, ignoring it for signing", source)
        return None
    return raw


def generated_key_id(mode: OperatingMode) -> str:
    return f"{PROVISIONED_HMAC_KEY_ID}-{mode.value}"


def _generated_key_path(mode: OperatingMode) -> Path:
    return Path(os.getenv(DATA_DIR_ENV, "data")).resolve() / f"{GENERATED_KEY_FILE_PREFIX}.{mode.value}"


def _read_generated_key(mode: OperatingMode) -> Optional[bytes]:
    try:
        raw = _generated_key_path(mode).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None

    return _key_bytes(raw) if raw != "" else None


def _write_generated_key(mode: OperatingMode) -> bytes:
    raw = secrets.token_hex(GENERATED_KEY_BYTES)
    path = _generated_key_path(mode)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        # Another process started concurrently and created the key first.
        concurrent_key = _read_generated_key(mode)
        if concurrent_key is not None:
            return concurrent_key
        raise RuntimeError(
            f"Cannot read the audit HMAC key at {path}: the file exists but is empty or "
            f"unreadable. Delete {path}, or let provisioning deliver AUDIT_HMAC_KEY and "
            "AUDIT_HMAC_KEY_ID."
        ) from error
    except OSError as error:
        raise RuntimeError(
            f"Cannot create the audit HMAC key at {path} ({error}). Provisioning provides "
            "AUDIT_HMAC_KEY and AUDIT_HMAC_KEY_ID; export them or make the data directory writable."
        ) from error

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(raw)
    except OSError as error:
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Cannot create the audit HMAC key at {path} ({error}). Provisioning provides "
            "AUDIT_HMAC_KEY and AUDIT_HMAC_KEY_ID; export them or make the data directory writable."
        ) from error

    return _key_bytes(raw)


def _resolve_signing_key() -> Tuple[bytes, str]:
    """Resolve the signing key and the key id stamped on new rows, once per process.

    An explicit key from provisioning keeps the id provisioning names, defaulting to
    `v1` because that is the id rows written before key rotation carry. A key
    generated here carries the mode-scoped id from `generated_key_id`.
    """
    global _HMAC_KEY_CACHE, _HMAC_KEY_ID

    env_key = _usable_key(os.getenv("AUDIT_HMAC_KEY"), "AUDIT_HMAC_KEY")
    configured_key = env_key or _usable_key(get_settings().auth.audit_hmac_key, "auth.audit_hmac_key")
    if configured_key is not None:
        key = _key_bytes(configured_key)
        key_id = os.getenv("AUDIT_HMAC_KEY_ID") or "v1"
    else:
        mode = get_operating_mode()
        key = _read_generated_key(mode) or _write_generated_key(mode)
        key_id = generated_key_id(mode)
        logger.warning(
            "No AUDIT_HMAC_KEY delivered by provisioning or config; signing audit rows with "
            "the self-generated key at %s under key id %s",
            _generated_key_path(mode),
            key_id,
        )

    _HMAC_KEY_CACHE = key
    _HMAC_KEY_ID = key_id

    return key, key_id


def _load_hmac_key() -> bytes:
    if _HMAC_KEY_CACHE is not None:
        return _HMAC_KEY_CACHE

    return _resolve_signing_key()[0]


def get_hmac_key_id() -> str:
    if _HMAC_KEY_ID is not None:
        return _HMAC_KEY_ID

    return _resolve_signing_key()[1]


def _ring_keys(key_id: str) -> List[bytes]:
    """Keys that may legitimately have produced a row stamped `key_id`.

    Includes keys generated for other modes, so rows that a development or demo
    workspace inherited from a copied production database still verify.
    """
    if key_id in _RING_KEY_CACHE:
        return _RING_KEY_CACHE[key_id]

    keys = list(PUBLISHED_HMAC_KEYS.get(key_id, []))
    for mode in OperatingMode:
        if generated_key_id(mode) != key_id:
            continue
        generated_key = _read_generated_key(mode)
        if generated_key is not None and generated_key not in keys:
            keys.append(generated_key)

    _RING_KEY_CACHE[key_id] = keys

    return keys


def reset_generated_key_cache() -> None:
    """Drop cached ring reads after a key file is added or replaced."""
    _RING_KEY_CACHE.clear()


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

    for legacy_key in _ring_keys(key_id) if key_id is not None else []:
        if _mac_matches(stored_mac, hmac_sha256_hex(message, legacy_key)):
            return None

    if key_id is not None and key_id != get_hmac_key_id() and len(_ring_keys(key_id)) == 0:
        return f"HMAC mismatch (no key available for key id {key_id})"

    # The id is one this stack knows, so a mac matching none of its keys is tampering.
    return "HMAC mismatch"
