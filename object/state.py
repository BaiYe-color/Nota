"""Pipeline 内部的数据契约。所有节点共享这三个 TypedDict。"""
from typing import List, TypedDict


class ParsedSlide(TypedDict):
    slide_id: int
    title: str
    body_text: str
    image_descriptions: List[str]
    key_visuals: List[dict]  # [{"description": str, "path": str, "bbox": [...], "content_key": str, "role": str}, ...]


class Card(TypedDict):
    card_id: str
    title: str
    core_concepts: List[str]
    key_points: List[str]
    formulas: List[str]
    examples: List[str]
    visual_hints: List[dict]  # [{"description": str, "path": str}, ...]
    importance: int
    source_slide_ids: List[int]


class NoteState(TypedDict):
    source_path: str
    parsed_slides: List[ParsedSlide]
    cards: List[Card]
    notes_markdown: str
