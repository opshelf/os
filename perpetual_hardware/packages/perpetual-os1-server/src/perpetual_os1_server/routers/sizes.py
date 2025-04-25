from fastapi import APIRouter, Depends
from perpetual_os1_server.core.printer import generate_random_upca
from perpetual_os1_server.core.sizes import ItemInformation, get_size_db, SizeDatabase
from pydantic import BaseModel

router = APIRouter()


@router.get("/")
async def get_size_data(size_database: SizeDatabase = Depends(get_size_db)) -> SizeDatabase:
    return size_database

class UpdateSizeRequest(BaseModel):
    upca: str
    item: ItemInformation

@router.put("/")
async def add_size_item(
    request: UpdateSizeRequest,
    size_database: SizeDatabase = Depends(get_size_db)
) -> SizeDatabase:
    size_database.add_item_size(request.upca, request.item.data, request.item.size)
    size_database.save()
    return size_database

@router.post("/")
async def add_item(
    item: ItemInformation,
    size_database: SizeDatabase = Depends(get_size_db)
) -> SizeDatabase:
    upca = generate_random_upca()
    size_database.add_item_size(upca, item.data, item.size)
    size_database.save()
    return size_database


@router.delete("/{upca}")
async def remove_item(
    upca: str, size_database: SizeDatabase = Depends(get_size_db)
) -> SizeDatabase:
    size_database.remove_item_size(upca)
    size_database.save()
    return size_database
