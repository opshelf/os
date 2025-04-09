from typing import Literal, Optional
from pydantic import BaseModel

class GS1Raw(BaseModel):
    gs1: str

class LabelScannedPayload(BaseModel):
    gs1: str
    location: Optional[str] = None

class MessagePayload(BaseModel):
    message: str

class LabelPrintPayload(BaseModel):
    gs1: str
    order_id: str
    quantity: int


class LabelPrintEvent(BaseModel):
    kind: Literal["label_printing_requested"] = "label_printing_requested"
    payload: LabelPrintPayload
