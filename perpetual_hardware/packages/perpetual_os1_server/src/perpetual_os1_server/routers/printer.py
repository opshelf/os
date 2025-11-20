from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import List, Tuple

from fastapi import APIRouter, Depends, HTTPException, status

from perpetual_os1_server.core.printer import LabelConfiguration, create_label
from perpetual_os1_server.routers.auth_deps import require_permissions
from perpetual_os1_server.settings import get_settings

router = APIRouter(tags=["printer"])


def _asset_paths() -> Tuple[str, str, str]:
    base_dir = Path(__file__).resolve().parents[3]
    logo_path = base_dir / "logo.png"
    barcode_path = base_dir / "barcode.png"
    output_path = base_dir / "label.png"
    return str(logo_path), str(barcode_path), str(output_path)


def _build_attributes(upca: str) -> List[Tuple[str, str]]:
    return [
        ("UPCA:", upca),
    ]


def _build_label_configuration(upca: str) -> LabelConfiguration:
    logo_path, barcode_path, output_path = _asset_paths()
    attributes = _build_attributes(upca)
    return LabelConfiguration(
        upca=upca,
        logo_path=logo_path,
        barcode_path=barcode_path,
        output_path=output_path,
        attributes=attributes,
    )


def _dispatch_to_printer(output_path: str) -> None:
    command = [
        "lpr",
        "-o",
        "media=Custom3x2",
        "-o",
        "fit-to-page",
        "-o",
        "orientation-requested=1",
    ]
    settings = get_settings()
    queue = settings.printer.queue or os.getenv("PRINTER")
    if queue:
        command.extend(["-P", queue])

    command.append(output_path)

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        logging.error("Print command not found: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Printer command unavailable",
        ) from exc

    if result.returncode != 0:
        logging.error("Printer command failed: %s", result.stderr.strip())
        if "No default destination" in (result.stderr or ""):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Printer command failed: no default destination (configure printer queue)",
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Printer command failed",
        )


@router.post(
    "/{upca}",
    dependencies=[Depends(require_permissions("printer:print"))],
)
async def print_label(upca: str) -> dict[str, str]:
    settings = get_settings()
    config = _build_label_configuration(upca)

    try:
        create_label(config)
    except Exception as exc:
        logging.exception("Failed to generate label for %s", upca)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate label",
        ) from exc

    printed = False
    if settings.printer.enabled:
        _dispatch_to_printer(config.output_path)
        printed = True
    else:
        logging.info("Printer disabled; generated label at %s", config.output_path)

    return {"message": "success", "upca": upca, "printed": printed or settings.printer.enabled}
