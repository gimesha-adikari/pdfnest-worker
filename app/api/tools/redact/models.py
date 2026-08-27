from pydantic import BaseModel


class RedactBox(BaseModel):
    id: str = ""
    page_id: str = ""
    page: int
    x: float
    y: float
    width: float
    height: float
