"""Size helpers now backed exclusively by the database.

The old JSON (size.json/items.json) compatibility layer has been removed. These
helpers provide legacy response shapes without touching disk.
"""

from __future__ import annotations

from typing import Dict, Iterable, Mapping, Any, cast

from pydantic import BaseModel, Field

from ..types import Dimensions, Json


class SerializableDimensions(Dimensions):
    def __getitem__(self, item: str) -> float:
        return float(getattr(self, item))


def _coerce_dimensions(dimensions: Dimensions | Mapping[str, float]) -> SerializableDimensions:
    if isinstance(dimensions, SerializableDimensions):
        return dimensions
    if isinstance(dimensions, Dimensions):
        data = dimensions.model_dump()
    else:
        data = dict(dimensions)
    return SerializableDimensions(**data)


class ItemInformation(BaseModel):
    size: SerializableDimensions
    data: Json


class SizeDatabase(BaseModel):
    """Lightweight in-memory representation shaped like the legacy JSON."""

    items: Dict[str, ItemInformation] = Field(default_factory=dict)


def build_size_database_from_items(items: Iterable[Any]) -> SizeDatabase:
    """Convert database Item rows into the legacy SizeDatabase shape."""
    payload: Dict[str, ItemInformation] = {}
    for item in items:
        payload[str(item.upca)] = ItemInformation(
            size=_coerce_dimensions(item.dimensions),
            data={
                "name": item.name,
                "weight": float(item.weight) if item.weight else 0,
                "metadata": cast(dict[str, Any], item.item_metadata or {}),
                "shelf_idx": "",
                "cell_idx": "",
            },
        )
    return SizeDatabase(items=payload)


def init_file_sizes(force: bool = False) -> None:
    """Legacy no-op kept for compatibility with application startup hooks."""
    return None


async def migrate_if_needed(session: Any) -> None:
    """Legacy no-op placeholder: database is authoritative, so no JSON migration."""
    return None
