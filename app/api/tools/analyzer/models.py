from pydantic import BaseModel


class PageAnalysis(BaseModel):
    page: int
    kind: str
    hasSelectableText: bool
    wordCount: int
    textBlockCount: int
    imageBlockCount: int
    textAreaRatio: float
    imageAreaRatio: float


class PDFAnalysis(BaseModel):
    pageCount: int
    pages: list[PageAnalysis]