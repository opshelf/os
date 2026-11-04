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


def test_demo_fridge_is_available_for_demo_and_development(monkeypatch) -> None:
	module = _import_operating_mode_module(monkeypatch)

	assert module.should_enable_demo_fridge(module.OperatingMode.development) is True
	assert module.should_enable_demo_fridge(module.OperatingMode.demo) is True
	assert module.should_enable_demo_fridge(module.OperatingMode.mock) is False
	assert module.should_enable_demo_fridge(module.OperatingMode.production) is False
