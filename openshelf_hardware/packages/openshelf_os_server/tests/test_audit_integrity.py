"""Audit signing key resolution and the legacy key ring."""

import hashlib
import pathlib
from types import SimpleNamespace
from typing import Optional

import pytest

from openshelf_os.config import settings as settings_module
from openshelf_os.operating_mode import OperatingMode
from openshelf_os_server.utils import audit_integrity


BINDING = audit_integrity.build_binding_message(7, "prev-hash", "entry-hash")
PUBLISHED_KEY = b"INSECURE_AUDIT_HMAC_KEY"
ROTATED_KEY = b"rotated-audit-key-0123456789abcdef"
OPERATOR_KEY = b"operator-strong-secret-0123456789abcdef"


@pytest.fixture(autouse=True)
def _reset_key_state(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: pathlib.Path,
) -> None:
	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_CACHE", None)
	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_ID", None)
	monkeypatch.setenv("OPENSHELF_DATA_DIR", str(tmp_path))
	monkeypatch.setattr(audit_integrity, "get_operating_mode", lambda: OperatingMode.mock)
	audit_integrity.reset_generated_key_cache()


def _stub_settings(
	monkeypatch: pytest.MonkeyPatch,
	audit_hmac_key: Optional[str],
) -> None:
	monkeypatch.setattr(
		audit_integrity,
		"get_settings",
		lambda: SimpleNamespace(auth=SimpleNamespace(audit_hmac_key=audit_hmac_key)),
	)
	monkeypatch.delenv("AUDIT_HMAC_KEY", raising=False)
	monkeypatch.delenv("AUDIT_HMAC_KEY_ID", raising=False)


def _restart(monkeypatch: pytest.MonkeyPatch, mode: OperatingMode) -> None:
	"""Drop the resolved key the way a new process would and switch mode."""
	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_CACHE", None)
	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_ID", None)
	monkeypatch.setattr(audit_integrity, "get_operating_mode", lambda: mode)
	audit_integrity.reset_generated_key_cache()


@pytest.mark.parametrize("published_key", sorted(settings_module.INSECURE_PUBLISHED_SECRETS))
def test_published_default_key_never_signs(
	monkeypatch: pytest.MonkeyPatch,
	published_key: str,
) -> None:
	_stub_settings(monkeypatch, audit_hmac_key=None)
	monkeypatch.setenv("AUDIT_HMAC_KEY", published_key)

	key = audit_integrity._load_hmac_key()

	assert key != published_key.encode("utf-8")
	assert audit_integrity.get_hmac_key_id() == "v2-mock"
	published_mac = audit_integrity.hmac_sha256_hex(BINDING, published_key.encode("utf-8"))
	assert audit_integrity.verify_binding_hmac(published_mac, BINDING, "v2-mock") == "HMAC mismatch"


def test_key_published_only_as_a_digest_never_signs(monkeypatch: pytest.MonkeyPatch) -> None:
	published_key = "key-published-in-an-old-revision"
	digest = hashlib.sha256(published_key.encode("utf-8")).hexdigest()
	monkeypatch.setattr(settings_module, "INSECURE_PUBLISHED_SECRET_DIGESTS", frozenset({digest}))
	_stub_settings(monkeypatch, audit_hmac_key=None)
	monkeypatch.setenv("AUDIT_HMAC_KEY", published_key)

	key = audit_integrity._load_hmac_key()

	assert key != published_key.encode("utf-8")
	assert audit_integrity.get_hmac_key_id() == "v2-mock"


def test_generated_key_is_persisted_and_survives_a_restart(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: pathlib.Path,
) -> None:
	_stub_settings(monkeypatch, audit_hmac_key=None)

	first_mac = audit_integrity.hmac_sha256_hex(BINDING)
	key_file = tmp_path / "audit-hmac-key.mock"

	assert audit_integrity.get_hmac_key_id() == "v2-mock"
	assert key_file.exists()
	assert key_file.stat().st_mode & 0o777 == 0o600

	_restart(monkeypatch, OperatingMode.mock)
	assert audit_integrity.hmac_sha256_hex(BINDING) == first_mac


def test_generated_key_ids_never_carry_the_provisioned_id(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	_stub_settings(monkeypatch, audit_hmac_key=None)

	for mode in OperatingMode:
		assert audit_integrity.generated_key_id(mode) != audit_integrity.PROVISIONED_HMAC_KEY_ID

	# A row that matches none of the current generated id's keys is still plain
	# tampering, not missing-key loss.
	current_id = audit_integrity.get_hmac_key_id()
	assert audit_integrity.verify_binding_hmac("0" * 64, BINDING, current_id) == "HMAC mismatch"


def test_preexisting_empty_key_file_reports_the_provisioning_contract(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: pathlib.Path,
) -> None:
	_stub_settings(monkeypatch, audit_hmac_key=None)
	key_file = tmp_path / "audit-hmac-key.mock"
	key_file.write_bytes(b"")

	with pytest.raises(RuntimeError, match="AUDIT_HMAC_KEY and AUDIT_HMAC_KEY_ID") as excinfo:
		audit_integrity._load_hmac_key()

	assert str(key_file) in str(excinfo.value)


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


def test_rows_inherited_from_a_copied_production_database_verify(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: pathlib.Path,
) -> None:
	_stub_settings(monkeypatch, audit_hmac_key=None)
	monkeypatch.setattr(audit_integrity, "get_operating_mode", lambda: OperatingMode.production)

	production_mac = audit_integrity.hmac_sha256_hex(BINDING)
	assert audit_integrity.get_hmac_key_id() == "v2-production"

	# A development workspace is forked by copying the production database, audit_logs
	# included, so those rows arrive stamped v2-production while development signs with
	# its own key.
	_restart(monkeypatch, OperatingMode.development)

	assert audit_integrity.get_hmac_key_id() == "v2-development"
	assert audit_integrity.verify_binding_hmac(production_mac, BINDING, "v2-production") is None
	assert audit_integrity.verify_binding_hmac("0" * 64, BINDING, "v2-production") == "HMAC mismatch"
