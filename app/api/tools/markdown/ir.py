from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class BlockSource(str, Enum):
    PYMUPDF_NATIVE = "pymupdf_native"
    TABLE_EXTRACTOR = "table_extractor"
    TESSERACT_OCR = "tesseract_ocr"


class ContentRole(str, Enum):
    PRIMARY = "primary"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    WATERMARK = "watermark"
    CAPTION = "caption"
    ANNOTATION = "annotation"


@dataclass
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    def intersects(self, other: "Rect") -> bool:
        return not (
            self.x1 < other.x0
            or self.x0 > other.x1
            or self.y1 < other.y0
            or self.y0 > other.y1
        )

    def iou(self, other: "Rect") -> float:
        inter_x0 = max(self.x0, other.x0)
        inter_y0 = max(self.y0, other.y0)
        inter_x1 = min(self.x1, other.x1)
        inter_y1 = min(self.y1, other.y1)
        if inter_x1 <= inter_x0 or inter_y1 <= inter_y0:
            return 0.0
        inter_area = (inter_x1 - inter_x0) * (inter_y1 - inter_y0)
        self_area = (self.x1 - self.x0) * (self.y1 - self.y0)
        other_area = (other.x1 - other.x0) * (other.y1 - other.y0)
        union_area = self_area + other_area - inter_area
        return inter_area / union_area if union_area > 0 else 0.0


@dataclass
class TextSpan:
    text: str
    is_bold: bool = False
    is_italic: bool = False
    is_code: bool = False
    font_size: float = 10.0
    font_name: str = ""
    link_url: Optional[str] = None


@dataclass
class IRNode:
    bbox: Rect
    page_number: int
    source: BlockSource = BlockSource.PYMUPDF_NATIVE
    role: ContentRole = ContentRole.PRIMARY
    reading_order_idx: int = 0
    confidence: float = 1.0


@dataclass
class HeadingNode(IRNode):
    level: int = 1
    spans: List[TextSpan] = field(default_factory=list)


@dataclass
class ParagraphNode(IRNode):
    spans: List[TextSpan] = field(default_factory=list)


@dataclass
class ListItemNode:
    spans: List[TextSpan] = field(default_factory=list)
    level: int = 0
    marker: str = "-"


@dataclass
class ListNode(IRNode):
    ordered: bool = False
    items: List[ListItemNode] = field(default_factory=list)


@dataclass
class TableCellNode:
    text: str = ""
    colspan: int = 1
    rowspan: int = 1
    align: str = "left"


@dataclass
class TableNode(IRNode):
    headers: List[TableCellNode] = field(default_factory=list)
    rows: List[List[TableCellNode]] = field(default_factory=list)
    alignments: List[str] = field(default_factory=list)
    upstream_origin: str = "MarkItDown adapted"


@dataclass
class CodeBlockNode(IRNode):
    code: str = ""
    language: str = ""


@dataclass
class ImageNode(IRNode):
    image_key: Optional[str] = None
    alt_text: str = "Figure"
    caption: Optional[str] = None


@dataclass
class PageBreakNode(IRNode):
    pass


@dataclass
class HeaderSemanticNode(IRNode):
    name_spans: List[TextSpan] = field(default_factory=list)
    title_spans: List[TextSpan] = field(default_factory=list)
    contact_spans: List[TextSpan] = field(default_factory=list)
    confidence: float = 1.0


@dataclass
class StructuredEntryNode(IRNode):
    title_spans: List[TextSpan] = field(default_factory=list)
    metadata_spans: List[TextSpan] = field(default_factory=list)
    body_nodes: List[IRNode] = field(default_factory=list)
    bullet_items: List[IRNode] = field(default_factory=list)
    confidence: float = 1.0
    entry_type: str = "generic"


@dataclass
class SectionGroupNode(IRNode):
    heading: Optional[HeadingNode] = None
    entries: List[IRNode] = field(default_factory=list)


@dataclass
class PageNode:
    page_number: int
    width: float
    height: float
    classification: str = "NATIVE"
    nodes: List[IRNode] = field(default_factory=list)


@dataclass
class DocumentIR:
    filename: str
    total_pages: int
    base_font_size: float = 10.0
    pages: List[PageNode] = field(default_factory=list)
