from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

import numpy as np

from app.api.tools.markdown.ir import IRNode, PageBreakNode, Rect, TableNode


@dataclass
class LayoutRegion:
    region_type: str  # "full_width", "multi_column"
    y0: float
    y1: float
    nodes: List[IRNode] = field(default_factory=list)


def partition_page_into_layout_regions(
    nodes: List[IRNode], page_width: float, page_height: float
) -> List[LayoutRegion]:
    """
    Partitions page into spatial LayoutRegion bands (full-width vs multi-column).
    Supports changing layout regions on the same page (e.g. H1 -> 2-col -> table -> 2-col -> footer).
    """
    if not nodes:
        return []

    sorted_nodes = sorted(nodes, key=lambda n: (round(n.bbox.y0, 1), round(n.bbox.x0, 1)))

    regions: List[LayoutRegion] = []
    current_region: LayoutRegion | None = None

    for node in sorted_nodes:
        width = node.bbox.x1 - node.bbox.x0
        spans = getattr(node, "spans", [])
        is_header_title = any(getattr(s, "font_size", 0) >= 14.0 for s in spans) and node.bbox.y0 <= 150.0
        is_full_width = width > 0.60 * page_width or is_header_title or isinstance(node, (TableNode, PageBreakNode))
        reg_type = "full_width" if is_full_width else "multi_column"

        if current_region is None:
            current_region = LayoutRegion(
                region_type=reg_type,
                y0=node.bbox.y0,
                y1=node.bbox.y1,
                nodes=[node],
            )
        elif current_region.region_type == reg_type:
            current_region.y1 = max(current_region.y1, node.bbox.y1)
            current_region.nodes.append(node)
        else:
            regions.append(current_region)
            current_region = LayoutRegion(
                region_type=reg_type,
                y0=node.bbox.y0,
                y1=node.bbox.y1,
                nodes=[node],
            )

    if current_region:
        regions.append(current_region)

    return regions


def sort_nodes_in_topological_reading_order(
    nodes: List[IRNode], page_width: float, page_height: float
) -> List[IRNode]:
    """
    Region-aware topological reading order sorter.
    Sequences document through spatial layout regions top-to-bottom:
    - Full-width regions read top-to-bottom.
    - Multi-column regions read Left Column top-to-bottom, then Right Column top-to-bottom.
    """
    if not nodes:
        return []

    regions = partition_page_into_layout_regions(nodes, page_width, page_height)
    final_ordered_nodes: List[IRNode] = []

    for region in regions:
        if region.region_type == "full_width":
            region.nodes.sort(key=lambda n: (round(n.bbox.y0, 1), round(n.bbox.x0, 1)))
            final_ordered_nodes.extend(region.nodes)
        else:
            left_nodes: List[IRNode] = []
            right_nodes: List[IRNode] = []
            gutter_x = 0.5 * page_width

            left_xs = [n.bbox.x1 for n in region.nodes if n.bbox.x1 < 0.48 * page_width]
            right_xs = [n.bbox.x0 for n in region.nodes if n.bbox.x0 > 0.35 * page_width]

            if left_xs and right_xs:
                gutter_x = (max(left_xs) + min(right_xs)) / 2.0

            for node in region.nodes:
                if node.bbox.x1 <= gutter_x + 15.0:
                    left_nodes.append(node)
                elif node.bbox.x0 >= gutter_x - 15.0:
                    right_nodes.append(node)
                else:
                    if node.bbox.x0 < gutter_x:
                        left_nodes.append(node)
                    else:
                        right_nodes.append(node)

            left_nodes.sort(key=lambda n: (round(n.bbox.y0, 1), round(n.bbox.x0, 1)))
            right_nodes.sort(key=lambda n: (round(n.bbox.y0, 1), round(n.bbox.x0, 1)))
            final_ordered_nodes.extend(left_nodes + right_nodes)

    for idx, n in enumerate(final_ordered_nodes):
        n.reading_order_idx = idx

    return final_ordered_nodes
