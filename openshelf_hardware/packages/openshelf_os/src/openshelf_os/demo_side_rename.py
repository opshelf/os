"""One-time, idempotent on-disk migration of pre-cutover demo mapping side letters.

Demo mapping files written before the front/back cutover key their cell
locations under the L/R side letters. This module renames them in place when a
reader loads the file, recording an exclusive-created backup of the original
bytes so a device picks the new letters up on its next load and the old file
stays recoverable. Files that already carry the new letters are left
untouched, so every demo.json reader migrates on load instead of accepting the
old letters in a live path.
"""

import os
import re
from typing import Any

from openshelf_os.common import write_json_file_with_backup
from openshelf_os.runtime_state import infer_mode_from_path

DEMO_SIDE_RENAME_BACKUP_SUFFIX = ".demo-side-rename.bak"

_DEMO_SIDE_RENAME_MAP = {
    "L": "F",
    "R": "B",
    "LEFT_MODULE": "FRONT_MODULE",
    "RIGHT_MODULE": "BACK_MODULE",
}
_DEMO_SIDE_RENAME_RE = re.compile(r"^(L|R|LEFT_MODULE|RIGHT_MODULE):(\d+):(\d+)$", re.IGNORECASE)


def rename_demo_side_string(value: str) -> str:
    """Rename the pre-cutover side letter in a three-part `SIDE:shelf:cell` string.

    Only location strings that match the pre-cutover side letters are rewritten;
    every other string passes through unchanged.
    """
    match = _DEMO_SIDE_RENAME_RE.match(value)
    if match is None:
        return value

    return f"{_DEMO_SIDE_RENAME_MAP[match.group(1).upper()]}:{match.group(2)}:{match.group(3)}"


def rename_demo_side_letters(node: Any) -> Any:
    """Walk the loaded mapping and rename side letters in location strings.

    Returns the same object when nothing needed renaming, so the caller can tell
    a no-op load from a migrated one.
    """
    if isinstance(node, dict):
        renamed: dict[Any, Any] = {}
        changed = False
        for key, value in node.items():
            new_key = rename_demo_side_string(key) if isinstance(key, str) else key
            new_value = rename_demo_side_letters(value)
            if (new_key is not key) or (new_value is not value):
                changed = True
            renamed[new_key] = new_value
        if changed is False:
            return node
        return renamed
    if isinstance(node, list):
        renamed_items = [rename_demo_side_letters(item) for item in node]
        if all(new is old for new, old in zip(renamed_items, node)):
            return node
        return renamed_items
    if isinstance(node, str):
        return rename_demo_side_string(node)
    return node


def write_with_demo_side_rename_backup(target_path: str, raw_content: str, data: Any) -> None:
    """Rewrite a demo mapping file, keeping an exclusive-created byte backup."""
    backup_path = target_path + DEMO_SIDE_RENAME_BACKUP_SUFFIX

    try:
        # Exclusive creation prevents a later invocation from overwriting the rollback copy.
        with open(backup_path, "xb") as stream:
            stream.write(raw_content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        # The backup is left over from an interrupted migration: trust it only when it
        # still holds the bytes this load read, otherwise the file changed on disk.
        with open(backup_path, "rb") as stream:
            if stream.read() != raw_content.encode("utf-8"):
                raise ValueError(
                    f"{target_path}: {DEMO_SIDE_RENAME_BACKUP_SUFFIX} does not match the "
                    f"current file; reconcile before migrating"
                )

    write_json_file_with_backup(
        target_path,
        data,
        backup_group_name="demo",
        backup_mode=infer_mode_from_path(target_path).value,
    )


def migrate_demo_side_rename(target_path: str, raw_content: str, data: Any) -> Any:
    """Rename the pre-cutover side letters in a demo mapping file, in place and idempotently.

    Files written before the front/back cutover keep their cell locations under the
    L/R side letters. Rename them in the loaded data and rewrite the file with an
    exclusive-created backup of the original bytes, so a device picks the new letters
    up on its next load and the old file is recoverable. Files that already carry
    the new letters are left untouched.
    """
    if isinstance(data, dict) is False:
        return data

    migrated = rename_demo_side_letters(data)
    if migrated is data:
        return data

    write_with_demo_side_rename_backup(target_path, raw_content, migrated)
    return migrated
