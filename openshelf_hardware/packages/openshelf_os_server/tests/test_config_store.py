"""The config store is the only writer of config.json; these cover the failures that would corrupt it,
leak a secret into it, or leave a device that cannot start."""

import json

import pytest

from openshelf_os.config.exceptions import ConfigRequirementError, ConfigValidationError
from openshelf_os.config.store import ConfigConflictError, ConfigStore

from openshelf_os_server import settings


@pytest.fixture
def config_path(tmp_path):
    document = settings._build_minimal_config({})
    document["x_unknown_key"] = {"kept": True}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document, indent="\t"))
    return path


def _store(path):
    return ConfigStore(path, overlay=settings._apply_env_overrides, check=settings.check_config_requirements)


def test_typed_update_writes_only_the_changed_field_never_the_env_overlay(config_path):
    store = _store(config_path)
    assert store.load().auth.jwt_secret == "test-jwt-secret"

    store.update(lambda draft: setattr(draft.printer, "feed_dots", 77))

    on_disk = json.loads(config_path.read_text())
    assert on_disk["printer"] == {"feed_dots": 77}
    assert "auth" not in on_disk
    assert on_disk["x_unknown_key"] == {"kept": True}
    assert "storage" not in on_disk


def test_invalid_save_leaves_file_and_loaded_config_untouched(config_path):
    store = _store(config_path)
    before = config_path.read_bytes()
    cached = store.load()

    cached.robot.tool.parameters.Z_MOVE_RPM = 0  # in-place edit, unvalidated by pydantic
    with pytest.raises(ConfigValidationError):
        store.save(cached)

    assert config_path.read_bytes() == before
    assert store.load().robot.tool.parameters.Z_MOVE_RPM == 1


def test_write_that_would_fail_startup_requirements_is_refused(config_path):
    store = _store(config_path)
    document = store.document()
    document.data["robot_app"] = {"enabled": True}
    document.data["robot"].pop("bus")
    before = config_path.read_bytes()

    with pytest.raises(ConfigRequirementError):
        store.replace(document.data, expected_revision=document.revision)

    assert config_path.read_bytes() == before


def test_replace_with_a_non_finite_number_is_refused_and_the_file_unchanged(config_path):
    store = _store(config_path)
    document = store.document()
    document.data["robot"]["grid"]["scan"] = {"front_bounds_x_min_mm": float("nan")}
    before = config_path.read_bytes()

    with pytest.raises(ConfigValidationError) as raised:
        store.replace(document.data, expected_revision=document.revision)

    assert raised.value.errors[0]["loc"] == ("robot", "grid", "scan", "front_bounds_x_min_mm")
    assert config_path.read_bytes() == before
    store.load()


def test_replace_refuses_a_document_read_before_the_file_changed(config_path):
    store = _store(config_path)
    document = store.document()
    external = json.loads(config_path.read_text())
    external["printer"] = {"feed_dots": 5}
    config_path.write_text(json.dumps(external))

    document.data["printer"] = {"feed_dots": 9}
    with pytest.raises(ConfigConflictError):
        store.replace(document.data, expected_revision=document.revision)

    assert json.loads(config_path.read_text())["printer"] == {"feed_dots": 5}


def test_typed_update_keeps_an_edit_made_on_disk_after_load(config_path):
    store = _store(config_path)
    store.load()
    external = json.loads(config_path.read_text())
    external["printer"] = {"feed_dots": 5}
    config_path.write_text(json.dumps(external))

    store.update(lambda draft: setattr(draft.robot_app, "home_on_init", False))

    on_disk = json.loads(config_path.read_text())
    assert on_disk["printer"] == {"feed_dots": 5}
    assert on_disk["robot_app"]["home_on_init"] is False


def test_generated_jwt_secret_survives_a_config_write(config_path, monkeypatch):
    # Without an env or file secret the overlay generates one; a new one per write would sign
    # every session out on each save.
    monkeypatch.delenv("JWT_SECRET")
    monkeypatch.setenv("ENV", "dev")
    store = _store(config_path)
    secret = store.load().auth.jwt_secret

    store.update(lambda draft: setattr(draft.printer, "feed_dots", 5))

    assert store.load().auth.jwt_secret == secret
    assert "auth" not in json.loads(config_path.read_text())


def test_typed_update_keeps_an_untouched_field_fed_by_a_legacy_shim(config_path):
    # robot.enabled is read as robot_app.enabled only while robot_app is absent; patching
    # robot_app.home_on_init alone would silently turn the robot off.
    document = json.loads(config_path.read_text())
    document.pop("robot_app")
    document["robot"]["enabled"] = True
    config_path.write_text(json.dumps(document))
    store = _store(config_path)
    assert store.load().robot_app.enabled is True

    store.update(lambda draft: setattr(draft.robot_app, "home_on_init", False))

    assert store.load().robot_app.enabled is True
    reloaded = _store(config_path).load()
    assert reloaded.robot_app.enabled is True
    assert reloaded.robot_app.home_on_init is False
    assert "auth" not in json.loads(config_path.read_text())
