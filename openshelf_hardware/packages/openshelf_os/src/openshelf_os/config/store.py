"""Single owner of the persisted config document.

The store keeps two views apart:

- the *document*: the dict that is on disk, after schema migration. Only this is ever written.
- the *effective* config: the document with the process overlay applied (env-provided auth values,
  dev-mode flags), validated into a RootConfig. This is what the application reads.

Writes from typed code (`update`, `save`) are diffed against the effective config they started
from, and only the changed fields are patched into the document. Overlay values, model defaults
and keys the model does not know are therefore never written by a change that did not touch
them. `replace` writes a whole document (the config editor) guarded by its revision.

Every write validates the candidate the same way startup does, holds one lock, rebases onto the
file if it changed on disk since it was read, and keeps a timestamped backup of the previous
bytes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from openshelf_os.config.exceptions import ConfigError, ConfigValidationError
from openshelf_os.config.file_io import write_json_file_with_backup
from openshelf_os.config.loader import load_and_validate_config
from openshelf_os.config.root import RootConfig
from openshelf_os.config_migrations.apply import load_config_json, migrate_config_file
from openshelf_os.runtime_state import paths_for_config_file

logger = logging.getLogger(__name__)

ABSENT_REVISION = "absent"

Overlay = Callable[[dict[str, Any]], dict[str, Any]]
Check = Callable[[RootConfig], None]
Mutation = Callable[[RootConfig], "RootConfig | None"]

_DELETE = object()
PathKey = tuple[str | int, ...]


class ConfigConflictError(ConfigError):
    """The file changed since the caller read the revision it is replacing."""


@dataclass(frozen=True)
class ConfigDocument:
    data: dict[str, Any]
    revision: str


def _revision(raw: bytes | None) -> str:
    if raw is None:
        return ABSENT_REVISION
    return hashlib.sha256(raw).hexdigest()[:16]


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _parse(path: Path, raw: bytes) -> dict[str, Any]:
    try:
        return load_config_json(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _non_finite_paths(node: Any, prefix: tuple[str | int, ...] = ()) -> list[tuple[str | int, ...]]:
    if isinstance(node, float):
        return [] if math.isfinite(node) else [prefix]
    if isinstance(node, dict):
        return [p for key, value in node.items() for p in _non_finite_paths(value, prefix + (key,))]
    if isinstance(node, list):
        return [p for index, value in enumerate(node) for p in _non_finite_paths(value, prefix + (index,))]
    return []


def diff_paths(before: Any, after: Any, prefix: PathKey = ()) -> list[tuple[PathKey, Any]]:
    """Leaf-level changes from `before` to `after` (JSON values); `_DELETE` marks a removed key."""
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[tuple[PathKey, Any]] = [
            (prefix + (key,), _DELETE) for key in before.keys() - after.keys()
        ]
        for key, value in after.items():
            if key in before:
                changes.extend(diff_paths(before[key], value, prefix + (key,)))
            else:
                changes.append((prefix + (key,), value))
        return changes
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        changes = []
        for index, (old, new) in enumerate(zip(before, after)):
            changes.extend(diff_paths(old, new, prefix + (index,)))
        return changes
    if type(before) is type(after) and before == after:
        return []
    return [(prefix, after)]


def _lookup(node: Any, path: PathKey) -> Any:
    for key in path:
        node = node[key]
    return node


def apply_changes(document: dict[str, Any], changes: list[tuple[PathKey, Any]], after: dict[str, Any]) -> None:
    """Patch `document` in place. Where its shape diverges from `after` (a legacy alias, a list of
    another length) the whole subtree at the divergence is taken from `after`."""
    for path, value in changes:
        node: Any = document
        for depth, key in enumerate(path[:-1]):
            child_ok = (
                isinstance(node, dict) and isinstance(key, str) and key in node
                or isinstance(node, list) and isinstance(key, int) and key < len(node)
            )
            next_key = path[depth + 1]
            if isinstance(node, dict) and key not in node and isinstance(next_key, str):
                node[key] = {}
                node = node[key]
                continue
            if child_ok:
                child = node[key]
                if isinstance(next_key, int) and isinstance(child, list) or isinstance(next_key, str) and isinstance(child, dict):
                    node = child
                    continue
            node[key] = copy.deepcopy(_lookup(after, path[: depth + 1]))
            break
        else:
            last = path[-1]
            if value is _DELETE:
                if isinstance(node, dict):
                    node.pop(last, None)
            else:
                node[last] = copy.deepcopy(value)


class ConfigStore:
    def __init__(
        self,
        path: Path,
        *,
        overlay: Overlay | None = None,
        check: Check | None = None,
        initial: dict[str, Any] | None = None,
    ) -> None:
        self.path = path
        self._overlay = overlay
        self._check = check
        self._initial = initial
        self._lock = threading.RLock()
        self._document: dict[str, Any] | None = None
        self._revision = ABSENT_REVISION
        self._config: RootConfig | None = None
        self._baseline: dict[str, Any] | None = None

    def effective(self, document: dict[str, Any]) -> RootConfig:
        """Validate a document as the application would load it; does not run write checks."""
        data = copy.deepcopy(document)
        if self._overlay is not None:
            data = self._overlay(data)
        return load_and_validate_config(data, str(self.path))

    def validate(self, document: dict[str, Any]) -> RootConfig:
        """Everything a write requires: model validation plus the startup requirements. A
        non-finite number is refused too: the boot reader rejects it."""
        bad = _non_finite_paths(document)
        if bad:
            raise ConfigValidationError(
                [{"loc": path, "msg": "Non-finite number"} for path in bad], str(self.path)
            )
        config = self.effective(document)
        if self._check is not None:
            self._check(config)
        return config

    def load(self) -> RootConfig:
        with self._lock:
            if self._config is not None:
                return self._config
            if self._document is None:
                self._read_document()
            assert self._document is not None
            self._install(self.effective(self._document))
            assert self._config is not None
            return self._config

    def document(self) -> ConfigDocument:
        """The document as it is on disk now (or the in-memory default when there is no file)."""
        with self._lock:
            raw = _read_bytes(self.path)
            if raw is None:
                self.load()
                assert self._document is not None
                return ConfigDocument(copy.deepcopy(self._document), ABSENT_REVISION)
            return ConfigDocument(_parse(self.path, raw), _revision(raw))

    def update(self, mutate: Mutation, *, version_id: str | None = None) -> RootConfig:
        """Apply `mutate` to a private copy of the effective config and persist what it changed."""
        with self._lock:
            draft = self.load().model_copy(deep=True)
            result = mutate(draft)
            return self._commit(draft if result is None else result, version_id)

    def save(self, config: RootConfig, *, version_id: str | None = None) -> RootConfig:
        """Persist the fields of `config` that differ from the effective config the store holds."""
        with self._lock:
            self.load()
            return self._commit(config, version_id)

    def replace(
        self,
        document: dict[str, Any],
        *,
        expected_revision: str,
        version_id: str | None = None,
    ) -> RootConfig:
        """Write a whole document, refusing when the file is not at `expected_revision`."""
        with self._lock:
            current = _revision(_read_bytes(self.path))
            if current != expected_revision:
                raise ConfigConflictError(f"{self.path} changed on disk since it was read")
            return self._write(copy.deepcopy(document), version_id)

    def _read_document(self) -> None:
        if self.path.exists() is True:
            try:
                changed, changes, backup = migrate_config_file(self.path)
            except (OSError, ValueError) as exc:
                logger.error("Config auto-migration failed: %s", exc, exc_info=exc)
                raise ConfigError(str(exc)) from None
            if changed:
                logger.info(
                    "Auto-migrated config %s (%s); rollback backup at %s",
                    self.path,
                    "; ".join(changes),
                    backup,
                )
        raw = _read_bytes(self.path)
        if raw is None:
            if self._initial is None:
                raise ConfigError(f"{self.path} does not exist")
            self._document = copy.deepcopy(self._initial)
            self._revision = ABSENT_REVISION
            return
        self._document = _parse(self.path, raw)
        self._revision = _revision(raw)

    def _install(self, config: RootConfig) -> None:
        self._config = config
        self._baseline = config.model_dump(mode="json")

    def _commit(self, config: RootConfig, version_id: str | None) -> RootConfig:
        assert self._baseline is not None and self._document is not None
        after = config.model_dump(mode="json")
        changes = diff_paths(self._baseline, after)
        if not changes:
            return self._config  # type: ignore[return-value]

        raw = _read_bytes(self.path)
        if _revision(raw) != self._revision and raw is not None:
            logger.warning("Config %s changed on disk outside the store; applying the change on top of it", self.path)
            document = _parse(self.path, raw)
        else:
            document = copy.deepcopy(self._document)
        try:
            # What the patched document must read back as: the document as it reads now, plus the change.
            intended = self.effective(document).model_dump(mode="json")
            apply_changes(intended, changes, after)
            apply_changes(document, changes, after)
            # A presence-keyed legacy shim (robot.enabled, io.curtain_enabled) can stop applying once
            # a patched field creates its new home; materialize what it used to supply.
            residual = diff_paths(self.effective(document).model_dump(mode="json"), intended)
            if residual:
                apply_changes(document, residual, intended)
                if diff_paths(self.effective(document).model_dump(mode="json"), intended):
                    raise ConfigError(f"The change did not take effect in {self.path}")
            return self._write(document, version_id)
        except Exception:
            # Drop any in-place edits a caller made to the cached instance.
            self._install(self.effective(self._document))
            raise

    def _write(self, document: dict[str, Any], version_id: str | None) -> RootConfig:
        config = self.validate(document)
        write_json_file_with_backup(
            self.path,
            document,
            indent="\t",
            backup_group_name="config",
            backup_mode=paths_for_config_file(self.path).mode.value,
            version_id=version_id,
        )
        self._document = document
        self._revision = _revision(_read_bytes(self.path))
        self._install(config)
        logger.info("Config saved to %s", self.path)
        return config
