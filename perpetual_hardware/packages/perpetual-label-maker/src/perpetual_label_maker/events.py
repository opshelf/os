from typing import Literal, Any, Dict, Annotated, Union
from pydantic import BaseModel, Field


class GS1Raw(BaseModel):
    gs1: str


class MessagePayload(BaseModel):
    message: str


class LabelPrintPayload(BaseModel):
    gs1: str
    order_id: str
    quantity: int


class LabelPrintEvent(BaseModel):
    kind: Literal["label_printing_requested"] = "label_printing_requested"
    payload: LabelPrintPayload


class LabelPrintingCompletedEvent(BaseModel):
    kind: Literal["label_printing_completed"] = "label_printing_completed"
    payload: Dict[Any, Any] = {}


class LabelPrintingFailedEvent(BaseModel):
    kind: Literal["label_printing_failed"] = "label_printing_failed"
    payload: MessagePayload


PrinterEvents = Annotated[
    Union[LabelPrintEvent, LabelPrintingCompletedEvent, LabelPrintingFailedEvent],
    Field(discriminator="kind"),
]
