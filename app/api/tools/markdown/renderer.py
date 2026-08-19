from __future__ import annotations

import re
from typing import List

from app.api.tools.markdown.ir import (
    CodeBlockNode,
    ContentRole,
    DocumentIR,
    HeadingNode,
    HeaderSemanticNode,
    StructuredEntryNode,
    SectionGroupNode,
    ImageNode,
    ListNode,
    PageBreakNode,
    ParagraphNode,
    TableNode,
    TextSpan,
)

MD_CONTROL_RE = re.compile(r"([\\*_#\[\]`|])")


def escape_markdown_prose(text: str) -> str:
    """Escapes Markdown control characters in literal prose spans."""
    return MD_CONTROL_RE.sub(r"\\\1", text)


class MarkdownRenderer:
    """AST-driven GitHub-Flavored Markdown renderer for DocumentIR."""

    @staticmethod
    def render(
        ir: DocumentIR,
        include_annotations: bool = False,
        escape_prose: bool = True,
        emit_page_breaks: bool = False,
    ) -> str:
        chunks: List[str] = []

        for page_idx, page in enumerate(ir.pages):
            if emit_page_breaks and page_idx > 0:
                chunks.append("\n<!-- pagebreak -->\n")

            for node in page.nodes:
                # Filter out running headers, footers, page numbers, and background watermarks
                if node.role in (
                    ContentRole.HEADER,
                    ContentRole.FOOTER,
                    ContentRole.PAGE_NUMBER,
                    ContentRole.WATERMARK,
                ):
                    continue

                if node.role == ContentRole.ANNOTATION and not include_annotations:
                    continue

                if isinstance(node, HeaderSemanticNode):
                    name_text = MarkdownRenderer._spans_to_md(node.name_spans, escape_prose).replace("**", "").strip()
                    title_text = MarkdownRenderer._spans_to_md(node.title_spans, escape_prose).replace("**", "").strip()
                    contact_text = MarkdownRenderer._spans_to_md(node.contact_spans, escape_prose)

                    if name_text:
                        chunks.append(f"\n# {name_text}\n")
                    if title_text:
                        chunks.append(f"\n## {title_text}\n")
                    if contact_text:
                        chunks.append(f"\n{contact_text}\n")

                elif isinstance(node, StructuredEntryNode):
                    title_text = MarkdownRenderer._spans_to_md(node.title_spans, escape_prose)
                    meta_text = MarkdownRenderer._spans_to_md(node.metadata_spans, escape_prose)

                    if title_text:
                        chunks.append(f"\n### {title_text}\n")
                    if meta_text:
                        chunks.append(f"\n*{meta_text}*\n")

                    for b_node in node.body_nodes:
                        b_spans = getattr(b_node, "spans", [])
                        b_text = MarkdownRenderer._spans_to_md(b_spans, escape_prose)
                        if b_text:
                            chunks.append(f"\n{b_text}\n")

                    for bl_node in node.bullet_items:
                        bl_md = MarkdownRenderer._render_list(bl_node, escape_prose) if isinstance(bl_node, ListNode) else ""
                        if bl_md:
                            chunks.append(f"\n{bl_md}\n")

                elif isinstance(node, SectionGroupNode):
                    if node.heading:
                        prefix = "#" * max(1, min(6, node.heading.level))
                        text = MarkdownRenderer._spans_to_md(node.heading.spans, escape_prose)
                        if text:
                            chunks.append(f"\n{prefix} {text}\n")
                    for entry in node.entries:
                        # Recursive render call for sub-entries
                        pass

                elif isinstance(node, HeadingNode):
                    prefix = "#" * max(1, min(6, node.level))
                    text = MarkdownRenderer._spans_to_md(node.spans, escape_prose)
                    if text:
                        chunks.append(f"\n{prefix} {text}\n")

                elif isinstance(node, ParagraphNode):
                    text = MarkdownRenderer._spans_to_md(node.spans, escape_prose)
                    if text:
                        if node.role == ContentRole.ANNOTATION:
                            chunks.append(f"\n> **[Comment]**: {text}\n")
                        else:
                            chunks.append(f"\n{text}\n")

                elif isinstance(node, ListNode):
                    list_md = MarkdownRenderer._render_list(node, escape_prose)
                    if list_md:
                        chunks.append(f"\n{list_md}\n")

                elif isinstance(node, TableNode):
                    table_md = MarkdownRenderer._render_table(node)
                    if table_md:
                        chunks.append(f"\n{table_md}\n")

                elif isinstance(node, CodeBlockNode):
                    lang = node.language or ""
                    chunks.append(f"\n```{lang}\n{node.code}\n```\n")

                elif isinstance(node, ImageNode):
                    caption = f"\n*{node.caption}*" if node.caption else ""
                    if node.image_key and node.image_key.startswith("data:"):
                        chunks.append(f"\n![{node.alt_text}]({node.image_key}){caption}\n")
                    else:
                        chunks.append(f"\n**{node.alt_text}**{caption}\n")

                elif isinstance(node, PageBreakNode):
                    if emit_page_breaks:
                        chunks.append("\n<!-- pagebreak -->\n")

        return "\n".join(chunks).strip() + "\n"

    @staticmethod
    def _spans_to_md(spans: List[TextSpan], escape_prose: bool) -> str:
        parts: List[str] = []
        for span in spans:
            text = span.text
            if not text:
                continue

            clean_text = escape_markdown_prose(text) if escape_prose else text

            if span.is_code:
                clean_text = f"`{clean_text}`"
            else:
                if span.is_bold and span.is_italic:
                    clean_text = f"***{clean_text}***"
                elif span.is_bold:
                    clean_text = f"**{clean_text}**"
                elif span.is_italic:
                    clean_text = f"*{clean_text}*"

            if span.link_url:
                clean_text = f"[{clean_text}]({span.link_url})"

            parts.append(clean_text)

        return " ".join(parts).strip()

    @staticmethod
    def _render_list(node: ListNode, escape_prose: bool) -> str:
        lines: List[str] = []
        for idx, item in enumerate(node.items):
            indent = "  " * item.level
            marker = f"{idx + 1}." if node.ordered else "-"
            text = MarkdownRenderer._spans_to_md(item.spans, escape_prose)
            lines.append(f"{indent}{marker} {text}")
        return "\n".join(lines)

    @staticmethod
    def _render_table(node: TableNode) -> str:
        if not node.headers and not node.rows:
            return ""

        all_rows = []
        if node.headers:
            all_rows.append([h.text for h in node.headers])
        for row in node.rows:
            all_rows.append([c.text for c in row])

        if not all_rows:
            return ""

        # Normalize column count across all rows
        num_cols = max(len(r) for r in all_rows)
        for r in all_rows:
            while len(r) < num_cols:
                r.append("")

        lines: List[str] = []

        # Header row
        header_str = "| " + " | ".join(all_rows[0]) + " |"
        lines.append(header_str)

        # Separator row
        sep_cells = []
        for i in range(num_cols):
            align = node.alignments[i] if i < len(node.alignments) else "left"
            if align == "center":
                sep_cells.append(":---:")
            elif align == "right":
                sep_cells.append("---:")
            else:
                sep_cells.append(":---")
        lines.append("| " + " | ".join(sep_cells) + " |")

        # Body rows
        for r in all_rows[1:]:
            # Convert internal line breaks to <br>
            clean_cells = [cell.replace("\n", "<br>").replace("|", "\\|") for cell in r]
            lines.append("| " + " | ".join(clean_cells) + " |")

        return "\n".join(lines)
