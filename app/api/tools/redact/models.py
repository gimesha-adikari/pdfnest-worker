from pydantic import BaseModel


class RedactBox(BaseModel):
    page: int
    x: float
    y: float
    width: float
    height: float