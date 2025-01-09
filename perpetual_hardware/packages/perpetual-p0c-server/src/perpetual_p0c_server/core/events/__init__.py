from typing import Literal, Any, Dict, Annotated, Union
from perpetual_p0c.robot import RetrievePayload
from pydantic import BaseModel, Field
from perpetual_p0c.storage import Dimensions, StorageModuleCellLocation


class GS1Raw(BaseModel):
    gs1: str


class MessagePayload(BaseModel):
    message: str


class StoringRequestedEvent(BaseModel):
    kind: Literal["storing_requested"] = "storing_requested"
    payload: GS1Raw


# class RetrievalPayload(BaseModel):
#    location: StorageModuleCellLocation


class RetrievalRequestedEvent(BaseModel):
    kind: Literal["retrieval_requested"] = "retrieval_requested"
    payload: RetrievePayload


class LabelPrintPayload(BaseModel):
    gs1: str
    order_id: str
    quantity: int


class LabelPrintEvent(BaseModel):
    kind: Literal["label_printing_requested"] = "label_printing_requested"
    payload: LabelPrintPayload


class LabelScannedEvent(BaseModel):
    kind: Literal["label_scanned"] = "label_scanned"
    payload: GS1Raw


class DimensionsPayload(Dimensions):
    weight: str


class InputPlacedEvent(BaseModel):
    kind: Literal["input_placed"] = "input_placed"
    payload: DimensionsPayload


class StoringCompletedEvent(BaseModel):
    kind: Literal["storing_completed"] = "storing_completed"
    payload: StorageModuleCellLocation


class StoringFailedEvent(BaseModel):
    kind: Literal["storing_failed"] = "storing_failed"
    payload: MessagePayload


class RetrievalCompletedEvent(BaseModel):
    kind: Literal["retrieval_completed"] = "retrieval_completed"
    payload: Dict[Any, Any] = {}


class RetrievalFailedEvent(BaseModel):
    kind: Literal["retrieval_failed"] = "retrieval_failed"
    payload: MessagePayload


class LabelPrintingCompletedEvent(BaseModel):
    kind: Literal["label_printing_completed"] = "label_printing_completed"
    payload: Dict[Any, Any] = {}


class LabelPrintingFailedEvent(BaseModel):
    kind: Literal["label_printing_failed"] = "label_printing_failed"
    payload: MessagePayload


UiEvents = Annotated[
    Union[
        StoringRequestedEvent,
        RetrievalRequestedEvent,
        LabelPrintEvent,
        LabelScannedEvent,
        InputPlacedEvent,
        StoringCompletedEvent,
        StoringFailedEvent,
        RetrievalCompletedEvent,
        RetrievalFailedEvent,
        LabelPrintingCompletedEvent,
        LabelPrintingFailedEvent,
    ],
    Field(discriminator="kind"),
]

PrinerEvents = Annotated[
    Union[LabelPrintEvent, LabelPrintingCompletedEvent, LabelPrintingFailedEvent],
    Field(discriminator="kind"),
]

StorageEvents = Annotated[
    Union[
        StoringRequestedEvent,
        RetrievalRequestedEvent,
        InputPlacedEvent,
        StoringCompletedEvent,
        StoringFailedEvent,
        RetrievalCompletedEvent,
        RetrievalFailedEvent,
    ],
    Field(discriminator="kind"),
]
