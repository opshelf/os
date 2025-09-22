from fastapi import APIRouter, Depends, HTTPException
from perpetual_os1_server.core.printer import create_barcode_upca, create_label, LabelConfiguration
from perpetual_os1.storage import Dimensions, Json
from perpetual_os1_server.core.sizes import get_size_db, SizeDatabase
from .auth_deps import get_current_user, require_permissions

import subprocess
from pydantic import BaseModel

router = APIRouter(tags=["printer"], dependencies=[Depends(get_current_user)])


class PrintRequest(BaseModel):
    dimensions: Dimensions
    item: Json


@router.post("/", dependencies=[Depends(require_permissions("printer:print"))])
async def print(pr: PrintRequest, size_database: SizeDatabase = Depends(get_size_db)):
    upca = create_label(LabelConfiguration())
    size_database.add_item_size(upca, pr.item, pr.dimensions)
    size_database.save()
    command = [
        "lpr",
        "-o",
        "media=Custom3x2",
        "-o",
        "fit-to-page",
        "-o",
        "orientation-requested=1",
        LabelConfiguration().output_path,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"Print failed: {result.stderr}")
    return {"message": "success"}


@router.post("/{upca}", dependencies=[Depends(require_permissions("printer:print"))])
async def print_upca(upca: str, size_database: SizeDatabase = Depends(get_size_db)):
    item_information = size_database.get_item_size(upca)
    if item_information is None:
        raise HTTPException(status_code=404, detail=f"Item with UPC-A '{upca}' not found")
    config = LabelConfiguration(
        upca=upca
    )
    upca = create_label(config)
    command = [
        "lpr",
        "-o",
        "media=Custom3x2",
        "-o",
        "fit-to-page",
        "-o",
        "orientation-requested=1",
        LabelConfiguration().output_path,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"Print failed: {result.stderr}")

    return {"message": "success"}


@router.post("/custom/{upca}", dependencies=[Depends(require_permissions("printer:print"))])
async def print_custom(upca: str, pr: PrintRequest, size_database: SizeDatabase = Depends(get_size_db)):
    size_database.add_item_size(upca, pr.item, pr.dimensions)
    size_database.save()
    config = LabelConfiguration(
        upca=upca
    )
    create_label(config)
    command = [
        "lpr",
        "-o",
        "media=Custom3x2",
        "-o",
        "fit-to-page",
        "-o",
        "orientation-requested=1",
        LabelConfiguration().output_path,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"Print failed: {result.stderr}")
    return {"message": "success"}
