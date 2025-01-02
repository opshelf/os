from abc import ABCMeta, abstractmethod
from typing import Optional, Literal, List
from pydantic import BaseModel
from enum import Enum


class ScannerType(str, Enum):
    CAMERA = "CAMERA"
    BARCODE_READER = "BARCODE_READER"


class BaseBarCode(BaseModel):
    barcode_type: Literal[
        "None",
        "Aztec",
        "Codabar",
        "Code39",
        "Code93",
        "Code128",
        "DataBar",
        "DataBarExpanded",
        "DataBarLimited",
        "DataMatrix",
        "DXFilmEdge",
        "EAN-8",
        "EAN-13",
        "ITF",
        "MaxiCode",
        "MicroQRCode",
        "PDF417",
        "QRCode",
        "rMQRCode",
        "UPC-A",
        "UPC-E",
        "Linear-Codes",
        "Matrix-Codes",
    ]
    payload: str


class BarCodeWithImage(BaseBarCode):
    image: bytes


class Scanner(metaclass=ABCMeta):
    @abstractmethod
    async def shutdown(self) -> None: ...

    @abstractmethod
    async def scan(self) -> Optional[List[BaseBarCode]]: ...

    @property
    @abstractmethod
    def scanner_type(self) -> ScannerType: ...
