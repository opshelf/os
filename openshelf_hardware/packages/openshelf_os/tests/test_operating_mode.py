from __future__ import annotations

import importlib
import sys

import sentry_sdk


def _import_operating_mode_module(monkeypatch):
	monkeypatch.setenv("OPERATING_MODE", "development")
	monkeypatch.setenv("SENTRY_DSN_HARDWARE", "")
	monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: None)
	monkeypatch.setattr(sentry_sdk, "set_tag", lambda *args, **kwargs: None)
	monkeypatch.setattr(sentry_sdk, "set_context", lambda *args, **kwargs: None)
	sys.modules.pop("openshelf_os.operating_mode", None)
	sys.modules.pop("openshelf_os", None)
	return importlib.import_module("openshelf_os.operating_mode")


def test_get_operating_mode_prefers_mode_file(tmp_path, monkeypatch) -> None:
	module = _import_operating_mode_module(monkeypatch)
	mode_file = tmp_path / ".mode"
	mode_file.write_text("demo", encoding="utf-8")

	monkeypatch.setenv("OPERATING_MODE", "production")

	assert module.get_operating_mode(mode_file) == module.OperatingMode.demo


def test_get_operating_mode_falls_back_to_env(tmp_path, monkeypatch) -> None:
	module = _import_operating_mode_module(monkeypatch)
	mode_file = tmp_path / ".mode"

	monkeypatch.setenv("OPERATING_MODE", "production")

	assert module.get_operating_mode(mode_file) == module.OperatingMode.production


def test_should_enable_sentry_only_for_demo_and_production(monkeypatch) -> None:
	module = _import_operating_mode_module(monkeypatch)

	assert module.should_enable_sentry(module.OperatingMode.development) is False
	assert module.should_enable_sentry(module.OperatingMode.demo) is True
	assert module.should_enable_sentry(module.OperatingMode.production) is True


def test_demo_and_production_share_production_traits(monkeypatch) -> None:
	module = _import_operating_mode_module(monkeypatch)

	assert module.has_production_traits(module.OperatingMode.development) is False
	assert module.has_production_traits(module.OperatingMode.demo) is True
	assert module.has_production_traits(module.OperatingMode.mock) is True
	assert module.has_production_traits(module.OperatingMode.production) is True
	assert module.should_enable_sentry(module.OperatingMode.mock) is False


def test_demo_features_are_enabled_only_in_demo_mode(monkeypatch) -> None:
	module = _import_operating_mode_module(monkeypatch)

	assert module.should_enable_demo_features(module.OperatingMode.development) is False
	assert module.should_enable_demo_features(module.OperatingMode.demo) is True
	assert module.should_enable_demo_features(module.OperatingMode.production) is False
