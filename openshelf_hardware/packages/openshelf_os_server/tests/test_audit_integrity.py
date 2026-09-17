"""Audit signing key resolution and the legacy key ring."""

from types import SimpleNamespace
from typing import Optional

import pytest

from openshelf_os_server.utils import audit_integrity


BINDING = audit_integrity.build_binding_message(7, "prev-hash", "entry-hash")
PUBLISHED_KEY = b"INSECURE_AUDIT_HMAC_KEY"
ROTATED_KEY = b"rotated-audit-key-0123456789abcdef"
OPERATOR_KEY = b"operator-strong-secret-0123456789abcdef"


@pytest.fixture(autouse=True)
def _reset_key_state(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_CACHE", None)
	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_ID", None)


def _stub_settings(
	monkeypatch: pytest.MonkeyPatch,
	audit_hmac_key: Optional[str],
	production_traits: bool = True,
) -> None:
	monkeypatch.setattr(
		audit_integrity,
		"get_settings",
		lambda: SimpleNamespace(auth=SimpleNamespace(audit_hmac_key=audit_hmac_key)),
	)
	monkeypatch.setattr(audit_integrity, "has_production_traits", lambda _mode: production_traits)


def test_published_default_key_never_signs(monkeypatch: pytest.MonkeyPatch) -> None:
	_stub_settings(monkeypatch, audit_hmac_key=None)
	monkeypatch.setenv("AUDIT_HMAC_KEY", "INSECURE_AUDIT_HMAC_KEY")

	with pytest.raises(RuntimeError, match="AUDIT_HMAC_KEY must be set"):
		audit_integrity.hmac_sha256_hex(BINDING)


def test_configured_key_signs_when_env_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
	_stub_settings(monkeypatch, audit_hmac_key="config-audit-key-0123456789abcdef")
	monkeypatch.delenv("AUDIT_HMAC_KEY", raising=False)

	expected = audit_integrity.hmac_sha256_hex(BINDING, b"config-audit-key-0123456789abcdef")
	assert audit_integrity.hmac_sha256_hex(BINDING) == expected


def test_dev_fallback_key_is_used_outside_production_traits(monkeypatch: pytest.MonkeyPatch) -> None:
	_stub_settings(monkeypatch, audit_hmac_key=None, production_traits=False)
	monkeypatch.delenv("AUDIT_HMAC_KEY", raising=False)

	assert audit_integrity._load_hmac_key() == b"development-audit-hmac-key-not-for-production"


def test_key_id_follows_the_configured_key_id_env(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "v3")
	assert audit_integrity.get_hmac_key_id() == "v3"

	# scripts/start exports an empty id when the operator supplies their own key.
	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_ID", None)
	monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "")
	assert audit_integrity.get_hmac_key_id() == "v1"


def test_rows_signed_with_published_keys_stay_verifiable_after_rotation(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_CACHE", ROTATED_KEY)
	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_ID", "v2")

	for published_key in audit_integrity.PUBLISHED_HMAC_KEYS["v1"]:
		legacy_mac = audit_integrity.hmac_sha256_hex(BINDING, published_key)
		assert audit_integrity.verify_binding_hmac(legacy_mac, BINDING, "v1") is None

	current_mac = audit_integrity.hmac_sha256_hex(BINDING, ROTATED_KEY)
	assert audit_integrity.verify_binding_hmac(current_mac, BINDING, "v2") is None

	# Rows written before hmac_key_id existed carry the key that was in use.
	assert audit_integrity.verify_binding_hmac(current_mac, BINDING, None) is None


def test_rows_signed_with_published_keys_stay_verifiable_under_operator_key(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	# An operator-supplied key without an explicit id also resolves to "v1",
	# the same id pre-rotation rows carry, so the ring must back them up.
	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_CACHE", OPERATOR_KEY)
	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_ID", "v1")

	for published_key in audit_integrity.PUBLISHED_HMAC_KEYS["v1"]:
		legacy_mac = audit_integrity.hmac_sha256_hex(BINDING, published_key)
		assert audit_integrity.verify_binding_hmac(legacy_mac, BINDING, "v1") is None

	current_mac = audit_integrity.hmac_sha256_hex(BINDING, OPERATOR_KEY)
	assert audit_integrity.verify_binding_hmac(current_mac, BINDING, "v1") is None

	# A row that matches neither the operator key nor the ring is ordinary tampering.
	assert audit_integrity.verify_binding_hmac("0" * 64, BINDING, "v1") == "HMAC mismatch"


def test_verify_reports_tampering_and_missing_keys(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_CACHE", ROTATED_KEY)
	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_ID", "v2")

	assert audit_integrity.verify_binding_hmac("0" * 64, BINDING, "v2") == "HMAC mismatch"
	assert audit_integrity.verify_binding_hmac(None, BINDING, "v2") == "HMAC mismatch"

	unrecorded_id = audit_integrity.verify_binding_hmac("0" * 64, BINDING, "v9")
	assert unrecorded_id is not None
	assert "no key available for key id v9" in unrecorded_id

	# A tampered row under an id the ring holds matches none of that id's
	# keys, so it is ordinary tampering, not missing-key loss.
	tampered_ring_mac = audit_integrity.hmac_sha256_hex(BINDING, OPERATOR_KEY)
	assert audit_integrity.verify_binding_hmac(tampered_ring_mac, BINDING, "v1") == "HMAC mismatch"

	# A row signed with the published key must not verify as a current-key row.
	published_mac = audit_integrity.hmac_sha256_hex(BINDING, PUBLISHED_KEY)
	assert audit_integrity.verify_binding_hmac(published_mac, BINDING, "v2") == "HMAC mismatch"
