from pydantic import BaseModel


class SignatureStamp(BaseModel):
    page: int
    x: float
    y: float
    width: float
    height: float