from fastapi import APIRouter, Depends
from perpetual_p0c.size_database import SizeDatabase
from perpetual_p0c_server.core.sizes import get_size_db
from perpetual_p0c.storage import GS1Data, Dimensions

router = APIRouter()


@router.get("/sizes")
async def get_size_data(size_database: SizeDatabase= Depends(get_size_db)) -> SizeDatabase:
    return size_database

@router.put("/sizes")
async def add_size_item(
    gs1_data: GS1Data,
    dimensions: Dimensions,
    size_database: SizeDatabase= Depends(get_size_db)
) -> SizeDatabase:
    size_database.add_item_size(gs1_data, dimensions)
    size_database.save()
    return size_database

@router.delete("/sizes")
async def remove_item(
    gs1_data: GS1Data,
    size_database: SizeDatabase= Depends(get_size_db)
) -> SizeDatabase:
    size_database.remove_item_size(gs1_data)
    size_database.save()
    return size_database
