from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from perpetual_os1_server.core.printer import generate_random_upca
from perpetual_os1_server.core.sizes import (
    ItemInformation,
    get_size_db,
    SizeDatabase,
    add_item_to_json,
    remove_item_from_json,
    get_item_in_json_format
)
from perpetual_os1_server.database.session import get_db
from pydantic import BaseModel
from .auth_deps import get_current_user, require_permissions

router = APIRouter()

# Apply authorization to specific endpoints instead of globally


@router.get("/", dependencies=[Depends(get_current_user), Depends(require_permissions("storage:read"))])
async def get_size_data(
    session: AsyncSession = Depends(get_db),
    use_json: bool = False
) -> SizeDatabase:
    """Get size data from database or JSON for backward compatibility."""
    if use_json:
        # Return original JSON format for hardware compatibility
        return get_size_db()

    # Get from database and convert to JSON format
    storage_service = get_storage_service()
    items = await storage_service.get_all_items()

    # Convert to legacy JSON format
    json_data = {}
    for item in items:
        item_info = ItemInformation(
            size=item.dimensions,
            data={
                "name": item.name,
                "weight": float(item.weight) if item.weight else 0,
                "metadata": item.item_metadata or {},
                "shelf_idx": None,
                "cell_idx": None
            }
        )
        json_data[item.upca] = item_info

    # Create legacy SizeDatabase object
    legacy_db = SizeDatabase()
    legacy_db.items = json_data
    return legacy_db


class UpdateSizeRequest(BaseModel):
    upca: str
    item: ItemInformation


@router.put("/", dependencies=[Depends(get_current_user), Depends(require_permissions("storage:write"))])
async def add_size_item(
    request: UpdateSizeRequest,
    session: AsyncSession = Depends(get_db),
    use_json: bool = False
) -> SizeDatabase:
    """Add size item to database and sync to JSON."""
    if use_json:
        # Use original JSON method for hardware compatibility
        add_item_to_json(request.upca, request.item.data, request.item.size)
        return get_size_db()

    # Use database with fallback
    storage_service = get_storage_service()

    try:
        # Create item in database
        await storage_service.create_item(
            upca=request.upca,
            name=request.item.data.get("name", "Unknown"),
            dimensions=request.item.size.model_dump(),
            weight=request.item.data.get("weight"),
            metadata=request.item.data.get("metadata", {})
        )

        # Sync to JSON for hardware compatibility
        await storage_service.sync_to_json()

        # Return updated data
        items = await storage_service.get_all_items()
        json_data = {}
        for item in items:
            item_info = ItemInformation(
                size=item.dimensions,
                data={
                    "name": item.name,
                    "weight": float(item.weight) if item.weight else 0,
                    "metadata": item.item_metadata or {},
                    "shelf_idx": None,
                    "cell_idx": None
                }
            )
            json_data[item.upca] = item_info

        legacy_db = SizeDatabase()
        legacy_db.items = json_data
        return legacy_db

    except Exception as e:
        import logging
        logging.error(f"Database operation failed for item {request.upca}: {str(e)}")
        # Fallback to JSON if database fails
        try:
            add_item_to_json(request.upca, request.item.data, request.item.size)
            return get_size_db()
        except Exception as json_e:
            logging.error(f"JSON fallback also failed for item {request.upca}: {str(json_e)}")
            raise RuntimeError(f"Both database and JSON operations failed for item {request.upca}") from json_e


@router.post("/", dependencies=[Depends(get_current_user), Depends(require_permissions("storage:write"))])
async def add_item(
    item: ItemInformation,
    session: AsyncSession = Depends(get_db),
    use_json: bool = False
) -> SizeDatabase:
    """Add item with generated UPCA to database and sync to JSON."""
    upca = generate_random_upca()

    if use_json:
        # Use original JSON method for hardware compatibility
        add_item_to_json(upca, item.data, item.size)
        return get_size_db()

    # Use database with fallback
    storage_service = get_storage_service()

    try:
        # Create item in database
        await storage_service.create_item(
            upca=upca,
            name=item.data.get("name", "Unknown"),
            dimensions=item.size.model_dump(),
            weight=item.data.get("weight"),
            metadata=item.data.get("metadata", {})
        )

        # Sync to JSON for hardware compatibility
        await storage_service.sync_to_json()

        # Return updated data
        items = await storage_service.get_all_items()
        json_data = {}
        for item in items:
            item_info = ItemInformation(
                size=item.dimensions,
                data={
                    "name": item.name,
                    "weight": float(item.weight) if item.weight else 0,
                    "metadata": item.item_metadata or {},
                    "shelf_idx": None,
                    "cell_idx": None
                }
            )
            json_data[item.upca] = item_info

        legacy_db = SizeDatabase()
        legacy_db.items = json_data
        return legacy_db

    except Exception as e:
        import logging
        logging.error(f"Database operation failed for new item: {str(e)}")
        # Fallback to JSON if database fails
        try:
            add_item_to_json(upca, item.data, item.size)
            return get_size_db()
        except Exception as json_e:
            logging.error(f"JSON fallback also failed for new item: {str(json_e)}")
            raise RuntimeError(f"Both database and JSON operations failed for new item") from json_e


@router.delete("/{upca}", dependencies=[Depends(get_current_user), Depends(require_permissions("storage:delete"))])
async def remove_item(
    upca: str,
    session: AsyncSession = Depends(get_db),
    use_json: bool = False
) -> SizeDatabase:
    """Remove item from database and sync to JSON."""
    if use_json:
        # Use original JSON method for hardware compatibility
        remove_item_from_json(upca)
        return get_size_db()

    # Use database with fallback
    storage_service = get_storage_service()

    try:
        # Delete from database
        success = await storage_service.delete_item(upca)

        if success:
            # Sync to JSON for hardware compatibility
            await storage_service.sync_to_json()

        # Return updated data
        items = await storage_service.get_all_items()
        json_data = {}
        for item in items:
            item_info = ItemInformation(
                size=item.dimensions,
                data={
                    "name": item.name,
                    "weight": float(item.weight) if item.weight else 0,
                    "metadata": item.item_metadata or {},
                    "shelf_idx": None,
                    "cell_idx": None
                }
            )
            json_data[item.upca] = item_info

        legacy_db = SizeDatabase()
        legacy_db.items = json_data
        return legacy_db

    except Exception as e:
        import logging
        logging.error(f"Database operation failed to remove item {upca}: {str(e)}")
        # Fallback to JSON if database fails
        try:
            remove_item_from_json(upca)
            return get_size_db()
        except Exception as json_e:
            logging.error(f"JSON fallback also failed to remove item {upca}: {str(json_e)}")
            raise RuntimeError(f"Both database and JSON operations failed to remove item {upca}") from json_e
