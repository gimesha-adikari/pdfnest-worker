from pydantic import BaseModel


class MetadataResponse(BaseModel):
    title: str = ""
    author: str = ""
    subject: str = ""
    keywords: str = ""