from typing import Literal

from openshelf_os.storage import StorageModuleSide


def module_name_to_side(side_name: Literal["left_module", "right_module"] | str) -> StorageModuleSide:
    if side_name == "left_module":
        return StorageModuleSide.LEFT

    return StorageModuleSide.RIGHT


def side_to_s(side: StorageModuleSide) -> int:
    if side == StorageModuleSide.LEFT:
        return 0

    return 1
