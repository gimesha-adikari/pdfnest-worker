from pydantic import BaseModel, Field


class R2ImageRef(BaseModel):
    key: str = Field(..., min_length=1)
    name: str = ""
    type: str = ""
    size: int = 0
