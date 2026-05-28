from typing import Annotated, Literal

from pydantic import BeforeValidator

from openshelf_os.storage import StorageModuleSide

StorageModuleSideName = Literal["left_module", "right_module"]


def module_name_to_side(side_name: StorageModuleSideName | str | StorageModuleSide) -> StorageModuleSide:
    if isinstance(side_name, StorageModuleSide):
        return side_name

    if side_name == "left":
        return StorageModuleSide.LEFT

    if side_name == "right":
        return StorageModuleSide.RIGHT

    try:
        return StorageModuleSide(side_name)
    except ValueError as exc:
        raise ValueError(f"Invalid storage module side: {side_name}") from exc


def side_to_s(side: StorageModuleSide) -> int:
    if side == StorageModuleSide.LEFT:
        return 0

    return 1

StorageModuleSideDto = Annotated[StorageModuleSide, BeforeValidator(module_name_to_side)]
