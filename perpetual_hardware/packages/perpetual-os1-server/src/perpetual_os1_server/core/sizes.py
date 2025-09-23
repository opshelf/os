"""Utilities for the legacy size database JSON compatibility layer."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, Mapping, Optional

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from perpetual_os1.storage import Dimensions, Json
from perpetual_os1_server.settings import get_settings


class SerializableDimensions(Dimensions):
    def __getitem__(self, item: str) -> float:
        return getattr(self, item)


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
    items: Dict[str, ItemInformation] = Field(default_factory=dict)
    filename: str = Field(default="data/size.json")

    @classmethod
    def load(cls, filename: str) -> "SizeDatabase":
        """Load size database from disk, accepting both legacy and new formats."""
        path = Path(filename)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - file system edge cases
            logging.warning("Unable to ensure size database directory %s: %s", path.parent, exc)

        if not path.exists():
            size_db = cls(filename=str(path))
            try:
                size_db.save()
            except OSError:
                logging.warning("Unable to create size database file at %s; operating in memory only", path)
            return size_db

        try:
            with path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError:
            logging.warning("Size database %s is not valid JSON; reinitializing", path)
            size_db = cls(filename=str(path))
            try:
                size_db.save()
            except OSError:
                logging.warning("Unable to create size database file at %s; operating in memory only", path)
            return size_db

        if isinstance(raw, dict) and "items" in raw and isinstance(raw["items"], dict):
            payload = raw["items"]
        elif isinstance(raw, dict):
            payload = raw
        else:
            payload = {}

        items: Dict[str, ItemInformation] = {}
        for upca, item_payload in payload.items():
            try:
                if isinstance(item_payload, ItemInformation):
                    info = ItemInformation(
                        size=_coerce_dimensions(item_payload.size),
                        data=item_payload.data,
                    )
                elif isinstance(item_payload, Mapping):
                    size_data = item_payload.get("size") or {}
                    data_payload = item_payload.get("data") or {}
                    info = ItemInformation(
                        size=_coerce_dimensions(size_data),
                        data=data_payload,
                    )
                else:
                    raise ValueError("Item payload must be a mapping")
            except Exception as exc:  # pragma: no cover - defensive path
                logging.warning("Skipping invalid size entry %s: %s", upca, exc)
                continue
            items[upca] = info

        return cls(items=items, filename=str(path))

    def get_item_size(self, upca: str) -> Optional[ItemInformation]:
        return self.items.get(upca)

    def add_item_size(self, upca: str, data: Json, dimensions: Dimensions) -> None:
        dims = _coerce_dimensions(dimensions)
        self.items[upca] = ItemInformation(size=dims, data=data or {})

    def remove_item_size(self, upca: str) -> None:
        self.items.pop(upca, None)

    def to_legacy_dict(self) -> Dict[str, Dict[str, Json]]:
        """Return payload using the historic JSON format expected by hardware tests."""
        serialized: Dict[str, Dict[str, Json]] = {}
        for upca, info in self.items.items():
            serialized[upca] = info.model_dump(mode="json")
        return serialized

    def save_to_file(self, filename: str) -> None:
        payload = self.to_legacy_dict()
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def save(self) -> None:
        self.save_to_file(self.filename)


size_database: Optional[SizeDatabase] = None
_size_db_path: Optional[str] = None


def _resolve_sizes_filename() -> str:
    env_path = os.getenv("SIZES_FILE")
    if env_path:
        return env_path
    return get_settings().sizes.filename


def get_sizes_filename() -> str:
    """Expose the active sizes filename for other modules."""
    return _resolve_sizes_filename()


def init_file_sizes(force: bool = False) -> None:
    """Initialise or reload the in-memory size database singleton."""
    global size_database, _size_db_path

    filename = _resolve_sizes_filename()
    if not force and size_database is not None and filename == _size_db_path:
        return

    size_database = SizeDatabase.load(filename)
    _size_db_path = filename


def get_storage_service(session: Optional[AsyncSession] = None):
    """Get real storage service for database operations."""
    # Import locally to avoid circular dependency at module load.
    from perpetual_os1_server.core.storage import StorageService

    if session is None:
        raise ValueError("Database session required for storage service operations")

    return StorageService(session)


def get_size_db() -> SizeDatabase:
    init_file_sizes()
    if size_database is None:  # pragma: no cover - safety net
        raise ValueError("Size Database was Not Initialized")
    return size_database




def get_item_in_json_format(upca: str) -> Optional[ItemInformation]:
    """Get item information in JSON format for backward compatibility."""
    size_db = get_size_db()
    return size_db.get_item_size(upca)


def add_item_to_json(upca: str, data: Json, dimensions: Dimensions) -> None:
    """Add item to JSON format for backward compatibility."""
    try:
        size_db = get_size_db()
        size_db.add_item_size(upca, data, dimensions)
        size_db.save()
    except Exception as e:
        import logging
        logging.error(f"Failed to add item {upca} to JSON: {str(e)}")
        raise RuntimeError(f"Failed to add item {upca} to JSON: {str(e)}") from e


def remove_item_from_json(upca: str) -> None:
    """Remove item from JSON format for backward compatibility."""
    try:
        size_db = get_size_db()
        size_db.remove_item_size(upca)
        size_db.save()
    except Exception as e:
        import logging
        logging.error(f"Failed to remove item {upca} from JSON: {str(e)}")
        raise RuntimeError(f"Failed to remove item {upca} from JSON: {str(e)}") from e


async def migrate_if_needed(session: AsyncSession) -> None:
    """Migrate JSON data to database if needed with proper locking."""
    import asyncio
    from sqlalchemy import text

    # Use file-based locking to prevent concurrent migrations
    filename = get_sizes_filename()
    lock_file = f"{filename}.lock"

    try:
        # Check if migration lock exists
        if os.path.exists(lock_file):
            # Wait up to 30 seconds for lock to be released
            for _ in range(30):
                if not os.path.exists(lock_file):
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("Migration locked by another process")

        # Create lock file
        with open(lock_file, "w") as f:
            f.write("locked")

        # Check if database has any items
        result = await session.execute(text("SELECT COUNT(*) FROM items"))
        db_count = result.scalar()

        # Check if JSON file exists and has data
        if os.path.exists(filename) and db_count == 0:
            import logging
            logging.info("Starting migration from JSON to database...")

            # Load JSON data
            size_db = get_size_db()
            if not size_db.items:
                logging.info("No items in JSON file to migrate")
                return

            # Import storage service
            from perpetual_os1_server.core.storage import StorageService
            storage_service = StorageService(session)

            # Migrate each item
            migrated_count = 0
            for upca, item_info in size_db.items.items():
                try:
                    await storage_service.create_item(
                        upca=upca,
                        name=item_info.data.get("name", "Unknown"),
                        dimensions=item_info.size.model_dump(),
                        weight=item_info.data.get("weight"),
                        metadata=item_info.data.get("metadata", {})
                    )
                    migrated_count += 1
                except Exception as e:
                    logging.error(f"Failed to migrate item {upca}: {str(e)}")
                    # Continue with other items

            logging.info(f"Migration completed: {migrated_count} items migrated")

            # Sync to JSON to maintain consistency
            await storage_service.sync_to_json()
        else:
            import logging
            logging.info("No migration needed")

    except Exception as e:
        import logging
        logging.error(f"Migration failed: {str(e)}")
        raise RuntimeError(f"Migration failed: {str(e)}") from e
    finally:
        # Remove lock file
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except Exception:
                pass  # Ignore lock file removal errors
