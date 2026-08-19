from __future__ import annotations

import re
import logging
from typing import List, Tuple, Optional, Dict, Any
from app.api.tools.markdown.ir import (
    IRNode,
    HeadingNode,
    ParagraphNode,
    ListNode,
    ListItemNode,
    TableNode,
    ImageNode,
    HeaderSemanticNode,
    StructuredEntryNode,
    SectionGroupNode,
    TextSpan,
    Rect,
    ContentRole,
)
from app.api.tools.markdown.layout import partition_page_into_layout_regions

logger = logging.getLogger(__name__)

# Patterns for contact items (email, phone, URLs, links)
CONTACT_PATTERNS = re.compile(
    r"(\+?\d[\d\s\-\(\)]{7,}|\b[\w\.-]+@[\w\.-]+\.\w+\b|linkedin\.com|github\.com|https?://|www\.|\b\d+,\s*[\w\s]+,\s*[\w\s]+\b)",
    re.I,
)

# Common entry metadata separators (bullets, slashes, pipes, dashes, dates)
ENTRY_META_SEPARATORS = re.compile(r"[•|/,–\-\u2022\u2013\u2014]|\b(19|20)\d{2}\b", re.I)


class SemanticGrouper:
    """
    Generic, confidence-based semantic grouping engine for DocumentIR.
    Uses dynamic spatial region decomposition, column clustering, and multi-signal confidence scoring.
    """

    @staticmethod
    def process_page_nodes(nodes: List[IRNode], page_width: float, page_height: float) -> List[IRNode]:
        if not nodes:
            return []

        # 1. Filter out small background decorative icons / logos (< 25px) without data URIs
        filtered_nodes: List[IRNode] = []
        for n in nodes:
            if isinstance(n, ImageNode):
                w = n.bbox.x1 - n.bbox.x0
                h = n.bbox.y1 - n.bbox.y0
                if w < 25.0 and h < 25.0 and not (n.image_key and n.image_key.startswith("data:")):
                    continue
            filtered_nodes.append(n)

        # 2. Decompose page into natural spatial layout regions (bands)
        regions = partition_page_into_layout_regions(filtered_nodes, page_width, page_height)
        
        final_nodes: List[IRNode] = []

        for reg_idx, region in enumerate(regions):
            if reg_idx == 0 and region.region_type == "full_width":
                # Check top region for Header (Name + Subtitle + Contact)
                header_node, remaining = SemanticGrouper._extract_header_semantic(region.nodes)
                if header_node:
                    final_nodes.append(header_node)
                region_nodes = remaining
            else:
                region_nodes = region.nodes

            if not region_nodes:
                continue

            if region.region_type == "full_width":
                # Full-width reading stream ordered top-to-bottom
                region_nodes.sort(key=lambda n: (round(n.bbox.y0, 1), round(n.bbox.x0, 1)))
                grouped = SemanticGrouper._group_reading_stream_confidence(region_nodes)
                final_nodes.extend(grouped)
            else:
                # Multi-column region: cluster into column streams
                columns = SemanticGrouper._cluster_nodes_into_columns(region_nodes, page_width)
                for col_nodes in columns:
                    col_nodes.sort(key=lambda n: (round(n.bbox.y0, 1), round(n.bbox.x0, 1)))
                    grouped = SemanticGrouper._group_reading_stream_confidence(col_nodes)
                    final_nodes.extend(grouped)

        return final_nodes

    @staticmethod
    def _cluster_nodes_into_columns(nodes: List[IRNode], page_width: float) -> List[List[IRNode]]:
        if not nodes:
            return []

        # Find column gutters using x-coordinate distribution
        gutter_x = 0.5 * page_width
        left_xs = [n.bbox.x1 for n in nodes if n.bbox.x1 < 0.48 * page_width]
        right_xs = [n.bbox.x0 for n in nodes if n.bbox.x0 > 0.35 * page_width]

        if left_xs and right_xs:
            gutter_x = (max(left_xs) + min(right_xs)) / 2.0

        left_nodes: List[IRNode] = []
        right_nodes: List[IRNode] = []

        for node in nodes:
            if node.bbox.x1 <= gutter_x + 15.0:
                left_nodes.append(node)
            elif node.bbox.x0 >= gutter_x - 15.0:
                right_nodes.append(node)
            else:
                if (node.bbox.x0 - 0) < (page_width - node.bbox.x1):
                    left_nodes.append(node)
                else:
                    right_nodes.append(node)

        cols: List[List[IRNode]] = []
        if left_nodes:
            cols.append(left_nodes)
        if right_nodes:
            cols.append(right_nodes)
        return cols if cols else [nodes]

    @staticmethod
    def _extract_header_semantic(nodes: List[IRNode]) -> Tuple[Optional[HeaderSemanticNode], List[IRNode]]:
        if not nodes:
            return None, []

        name_spans: List[TextSpan] = []
        title_spans: List[TextSpan] = []
        contact_spans: List[TextSpan] = []
        remaining_nodes: List[IRNode] = []

        max_font_size = 0.0
        name_node: Optional[IRNode] = None

        for n in nodes:
            spans = getattr(n, "spans", [])
            for s in spans:
                if s.font_size > max_font_size:
                    max_font_size = s.font_size
                    name_node = n

        header_consumed: List[IRNode] = []

        for n in nodes:
            spans = getattr(n, "spans", [])
            text = " ".join([s.text for s in spans]).strip()
            if not text:
                continue

            if n is name_node or any(s.font_size >= max_font_size - 1.0 for s in spans):
                name_spans.extend(spans)
                header_consumed.append(n)
            elif CONTACT_PATTERNS.search(text) or "address" in text.lower():
                contact_spans.extend(spans)
                header_consumed.append(n)
            elif any(s.font_size >= max_font_size - 6.0 for s in spans) and n.bbox.y0 <= 150.0:
                title_spans.extend(spans)
                header_consumed.append(n)
            else:
                remaining_nodes.append(n)

        if not name_spans and not title_spans and not contact_spans:
            return None, nodes

        header_node = HeaderSemanticNode(
            bbox=nodes[0].bbox,
            page_number=nodes[0].page_number,
            name_spans=name_spans,
            title_spans=title_spans,
            contact_spans=contact_spans,
            confidence=0.90,
        )
        return header_node, remaining_nodes

    @staticmethod
    def _group_reading_stream_confidence(nodes: List[IRNode], confidence_threshold: float = 0.60) -> List[IRNode]:
        if not nodes:
            return []

        grouped_result: List[IRNode] = []
        i = 0
        n_len = len(nodes)

        while i < n_len:
            node = nodes[i]
            if isinstance(node, (TableNode, ImageNode, HeadingNode)):
                grouped_result.append(node)
                i += 1
                continue

            score, signals = SemanticGrouper._evaluate_entry_title_confidence(node, nodes, i)

            if score >= confidence_threshold:
                # High confidence -> build generic StructuredEntryNode
                spans = getattr(node, "spans", [])
                title_spans = spans
                meta_spans: List[TextSpan] = []
                body_nodes: List[IRNode] = []
                bullet_items: List[IRNode] = []

                i += 1

                # Check if next node is metadata line
                if i < n_len:
                    next_node = nodes[i]
                    next_spans = getattr(next_node, "spans", [])
                    next_text = " ".join([s.text for s in next_spans]).strip()

                    has_meta_sep = bool(ENTRY_META_SEPARATORS.search(next_text))
                    is_short = len(next_text.split()) <= 16

                    if (has_meta_sep or any(s.is_italic for s in next_spans)) and is_short:
                        meta_spans = next_spans
                        i += 1

                # Collect body nodes belonging to this entry until next title/heading
                while i < n_len:
                    curr = nodes[i]
                    if isinstance(curr, (HeadingNode, TableNode)):
                        break

                    c_score, _ = SemanticGrouper._evaluate_entry_title_confidence(curr, nodes, i)
                    if c_score >= confidence_threshold:
                        break

                    if isinstance(curr, ListNode):
                        bullet_items.append(curr)
                    else:
                        body_nodes.append(curr)
                    i += 1

                entry_node = StructuredEntryNode(
                    bbox=node.bbox,
                    page_number=node.page_number,
                    title_spans=title_spans,
                    metadata_spans=meta_spans,
                    body_nodes=body_nodes,
                    bullet_items=bullet_items,
                    confidence=score,
                )
                grouped_result.append(entry_node)
            else:
                # Low confidence -> preserve original IR node without structural mutation
                grouped_result.append(node)
                i += 1

        return grouped_result

    @staticmethod
    def _evaluate_entry_title_confidence(node: IRNode, nodes: List[IRNode], idx: int) -> Tuple[float, Dict[str, float]]:
        """
        Calculates an explainable multi-signal confidence score for candidate entry title boundaries.
        Signals evaluated:
        - S_bold: Bold font presence (+0.35)
        - S_size: Font size >= base font size (+0.15)
        - S_length: Short line length <= 12 words (+0.15)
        - S_meta_follow: Followed by metadata/technology line (+0.25)
        - S_body_follow: Followed by body prose paragraphs (+0.10)
        """
        signals: Dict[str, float] = {
            "S_bold": 0.0,
            "S_size": 0.0,
            "S_length": 0.0,
            "S_meta_follow": 0.0,
            "S_body_follow": 0.0,
        }

        spans = getattr(node, "spans", [])
        text = " ".join([s.text for s in spans]).strip()

        if not text or len(text) > 120 or text.endswith("."):
            return 0.0, signals

        words = text.split()
        if len(words) > 12:
            return 0.0, signals

        # 1. Bold font signal
        if any(s.is_bold for s in spans):
            signals["S_bold"] = 0.35

        # 2. Font size signal
        if len(spans) > 0 and spans[0].font_size >= 10.0:
            signals["S_size"] = 0.15

        # 3. Short line length signal
        if len(words) <= 8:
            signals["S_length"] = 0.15

        # 4. Metadata line signal (next node)
        if idx + 1 < len(nodes):
            next_node = nodes[idx + 1]
            next_spans = getattr(next_node, "spans", [])
            next_text = " ".join([s.text for s in next_spans]).strip()

            if ENTRY_META_SEPARATORS.search(next_text) or any(s.is_italic for s in next_spans):
                if len(next_text.split()) <= 16:
                    signals["S_meta_follow"] = 0.25

        # 5. Body prose continuation signal (next+1 node)
        if idx + 2 < len(nodes):
            body_node = nodes[idx + 2]
            body_spans = getattr(body_node, "spans", [])
            body_text = " ".join([s.text for s in body_spans]).strip()
            if len(body_text.split()) > 10:
                signals["S_body_follow"] = 0.10

        total_score = sum(signals.values())
        return min(1.0, total_score), signals
