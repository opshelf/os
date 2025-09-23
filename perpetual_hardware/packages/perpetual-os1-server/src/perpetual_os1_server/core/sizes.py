from pydantic import BaseModel
from perpetual_os1.storage import Dimensions, Json
from typing import Dict, Iterable, Optional, Union, Mapping
import os
from typing import Optional
from perpetual_os1_server.settings import get_settings
from sqlalchemy.ext.asyncio import AsyncSession


class ItemInformation(BaseModel):
    size: Dimensions
    data: Json


class SizeDatabase(BaseModel):
    items: Dict[str, ItemInformation] = {}
    filename: str = "size.json"

    def get_item_size(self, data: str) -> Optional[ItemInformation]:
        return self.items.get(data)

    def add_item_size(self, upca: str, data: Json, dimensions: Dimensions) -> None:
        print(data, dimensions)
        self.items[upca] = ItemInformation(size=dimensions, data=data)
        print(self.items)

    def remove_item_size(self, upca: str) -> None:
        self.items.pop(upca, None)

    def load_from_file(self, filename: str) -> None:
        with open(filename) as f:
            result = self.model_validate_json(f.read())
            self.items = result.items

    def save_to_file(self, filename: str) -> None:
        with open(filename, "w+") as f:
            f.write(self.model_dump_json())

    def save(self) -> None:
        self.save_to_file(self.filename)


size_database: Optional[SizeDatabase] = None


def init_file_sizes() -> None:
    global size_database, _storage_service
    settings = get_settings()
    if os.path.isfile(settings.sizes.filename):
        with open(settings.sizes.filename) as f:
            size_database = SizeDatabase.model_validate_json(f.read())
    else:
        with open(settings.sizes.filename, "w+") as f:
            default_size_db = SizeDatabase(filename=settings.sizes.filename)
            f.write(default_size_db.model_dump_json())
            size_database = default_size_db


def get_storage_service():
    """Mock storage service for backward compatibility."""
    # This is a temporary mock to avoid circular imports
    # In production, this should be properly initialized
    class MockStorageService:
        async def get_all_items(self):
            # Return empty list for now
            return []

        async def create_item(self, upca: str, name: str, dimensions: dict, weight: float = None, metadata: dict = None):
            pass

        async def delete_item(self, upca: str):
            return True

        async def sync_to_json(self):
            pass
    return MockStorageService()


def get_size_db() -> SizeDatabase:
    if size_database:
        return size_database
    raise ValueError("Size Database was Not Initialized")




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
    from sqlalchemy import text, select
    from perpetual_os1_server.settings import get_settings

    # Use file-based locking to prevent concurrent migrations
    settings = get_settings()
    lock_file = f"{settings.sizes.filename}.lock"

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
        if os.path.exists(settings.sizes.filename) and db_count == 0:
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
