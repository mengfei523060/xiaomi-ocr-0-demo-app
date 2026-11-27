"""Region helpers extracted verbatim from evaluation_pipeline/layout_detect_paddlex.py.

The upstream module is a multi-GPU CLI that also imports tqdm/multiprocessing;
the App only needs PaddleX result → normalized region serialization, so those
functions live here without the extra dependencies. Logic is byte-for-byte
identical to upstream (extract_boxes / task_type_for_label / serialize_regions).
"""
from __future__ import annotations

from typing import Any

LABEL_TASK_MAPPING = {
    "text": [
        "abstract", "algorithm", "content", "doc_title", "figure_title",
        "paragraph_title", "reference_content", "text", "vertical_text",
        "vision_footnote", "seal", "formula_number", "header", "footer",
        "number", "footnote", "aside_text", "reference",
    ],
    "table": ["table"],
    "formula": ["display_formula", "inline_formula"],
    "skip": ["chart", "image", "footer_image", "header_image"],
    "abandon": [],
}


def task_type_for_label(label: str) -> str | None:
    for task_type, labels in LABEL_TASK_MAPPING.items():
        if label in labels:
            return task_type
    return None


def extract_boxes(result: Any) -> list[dict[str, Any]]:
    if hasattr(result, "get") and result.get("boxes") is not None:
        return result["boxes"]
    try:
        inner = result["res"]
    except (KeyError, TypeError, AttributeError):
        inner = None
    if hasattr(inner, "get") and inner.get("boxes") is not None:
        return inner["boxes"]
    json_value = getattr(result, "json", None)
    if callable(json_value):
        json_value = json_value()
    if isinstance(json_value, dict):
        if json_value.get("boxes") is not None:
            return json_value["boxes"]
        inner = json_value.get("res")
        if isinstance(inner, dict) and inner.get("boxes") is not None:
            return inner["boxes"]
    return []


def serialize_regions(result: Any, image_width: int, image_height: int) -> list[dict[str, Any]]:
    regions = []
    for box in extract_boxes(result):
        label = str(box.get("label", ""))
        coordinate = box.get("coordinate", [])
        if len(coordinate) != 4:
            continue
        task_type = task_type_for_label(label)
        if task_type is None or task_type == "abandon":
            continue
        x1, y1, x2, y2 = [float(value) for value in coordinate]
        x1 = max(0.0, min(x1, float(image_width)))
        y1 = max(0.0, min(y1, float(image_height)))
        x2 = max(0.0, min(x2, float(image_width)))
        y2 = max(0.0, min(y2, float(image_height)))
        if x2 <= x1 or y2 <= y1:
            continue
        bbox_2d = [
            int(x1 / image_width * 1000),
            int(y1 / image_height * 1000),
            int(x2 / image_width * 1000),
            int(y2 / image_height * 1000),
        ]
        x1_normalized, y1_normalized, x2_normalized, y2_normalized = bbox_2d
        regions.append({
            "index": len(regions),
            "label": label,
            "score": float(box.get("score", 0.0)),
            "bbox_2d": bbox_2d,
            "polygon": [
                [x1_normalized, y1_normalized],
                [x2_normalized, y1_normalized],
                [x2_normalized, y2_normalized],
                [x1_normalized, y2_normalized],
            ],
            "task_type": task_type,
        })
    return regions
