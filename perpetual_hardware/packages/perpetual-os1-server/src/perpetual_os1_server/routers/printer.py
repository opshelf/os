from fastapi import APIRouter, Response, Depends
from typing import AsyncGenerator, Optional
from perpetual_os1_server.core.printer import create_label, LabelConfiguration
from perpetual_os1.storage import Dimensions
from typing import Dict, Any
from perpetual_os1_server.core.sizes import get_size_db, SizeDatabase, Json
import subprocess
from pydantic import BaseModel

router = APIRouter()

class PrintRequest(BaseModel):
    dimensions: Dimensions
    item: Json


@router.post("/")
async def print(
    pr: PrintRequest,
    size_database: SizeDatabase = Depends(get_size_db)
):
    upca = create_label(LabelConfiguration())
    size_database.add_item_size(upca, pr.item, pr.dimensions)
    size_database.save()
    command = ["lpr", "-o", "media=Custom3x2", "-o", "fit-to-page", "-o", "orientation-requested=1", LabelConfiguration().output_path]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"Print failed: {result.stderr}")
    return { "message": "success" }
