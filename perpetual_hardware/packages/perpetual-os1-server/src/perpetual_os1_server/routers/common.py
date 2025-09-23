from pydantic import BaseModel


class SuccessResponse(BaseModel):
    message: str = "success"


class PaginationParams(BaseModel):
    skip: int = 0
    limit: int = 100
