from pydantic import BaseModel, Field
from enum import Enum
from typing import Optional, List


class EpistemicConfidence(str, Enum):
    CONFIRMED = "CONFIRMED"
    STRONGLY_INFERRED = "STRONGLY_INFERRED"
    WEAKLY_INFERRED = "WEAKLY_INFERRED"


class Evidence(BaseModel):
    id: str
    sourceType: str
    filePath: str
    lineStart: Optional[int] = None
    lineEnd: Optional[int] = None
    symbol: Optional[str] = None
    detector: str
    confidence: EpistemicConfidence
    description: str


class StructureNodeType(str, Enum):
    DIRECTORY = "directory"
    FILE = "file"


class StructureNode(BaseModel):
    path: str
    name: str
    type: StructureNodeType
    size: Optional[int] = None
    category: Optional[str] = None
    language: Optional[str] = None
    children: Optional[List['StructureNode']] = None


class ProjectStructure(BaseModel):
    rootName: str
    root: StructureNode
    totalFiles: int
    totalDirs: int


# Existing models for PDF Analysis
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