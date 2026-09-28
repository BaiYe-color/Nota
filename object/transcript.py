"""把 ASR 得到的 TranscriptSegment[] 聚合成 ParsedSlide[]，让下游 cards/writer 无缝复用。

策略：按时间窗口分块（默认每 3 分钟一块），每块作为一张 "slide"。
- title 留空（让 cards_node 从内容里自己起）
- body_text 是这一段的完整转录，含说话人标签（如启用）和时间戳
- 无图，image_descriptions=[] key_visuals=[]
"""
from typing import List

from asr import TranscriptSegment
from state import ParsedSlide


def _format_time(ms: int) -> str:
    """毫秒 → HH:MM:SS。"""
    s = ms // 1000
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _format_segment(seg: TranscriptSegment, show_speaker: bool) -> str:
    """把一段转录格式化为一行文本。"""
    ts = _format_time(seg["start_ms"])
    text = seg["text"]
    if show_speaker and seg.get("speaker_id") is not None:
        return f"[{ts}] 说话人{seg['speaker_id']}: {text}"
    return f"[{ts}] {text}"


def segments_to_slides(
    segments: List[TranscriptSegment],
    *,
    chunk_seconds: int = 180,
    show_speaker: bool = False,
) -> List[ParsedSlide]:
    """按时间窗口切分转录，每窗口打包成一张 ParsedSlide。

    chunk_seconds: 每片时长（秒）。默认 180 = 3 分钟。
    show_speaker: 是否在每段前显示说话人标签。
    """
    if not segments:
        return []

    slides: List[ParsedSlide] = []
    chunk_ms = chunk_seconds * 1000
    current_start = segments[0]["start_ms"]
    current_lines: list = []

    def _flush():
        if not current_lines:
            return
        slide_id = len(slides) + 1
        first_ts = _format_time(current_lines[0]["start_ms"])
        last_ts = _format_time(current_lines[-1]["end_ms"])
        title = f"{first_ts} - {last_ts}"
        body_lines = [_format_segment(s, show_speaker) for s in current_lines]
        slides.append({
            "slide_id": slide_id,
            "title": title,
            "body_text": "\n".join(body_lines),
            "image_descriptions": [],
            "key_visuals": [],
        })

    for seg in segments:
        # 时间跨越 chunk 边界就切片
        if seg["start_ms"] - current_start >= chunk_ms and current_lines:
            _flush()
            current_lines = []
            current_start = seg["start_ms"]
        current_lines.append(seg)
    _flush()

    return slides


def raw_transcript_markdown(
    segments: List[TranscriptSegment],
    *,
    show_speaker: bool = False,
) -> str:
    """把 segments 平铺成完整逐字稿 Markdown（不切片，全文按时间顺序）。

    用作 "只要逐字稿" / "逐字稿+纪要" 场景下的独立产物。
    """
    if not segments:
        return "# 逐字稿\n\n（无内容）"
    lines = ["# 逐字稿", ""]
    prev_speaker = None
    for seg in segments:
        if show_speaker and seg.get("speaker_id") is not None:
            sp = seg["speaker_id"]
            if sp != prev_speaker:
                lines.append("")  # 换人时插空行
                prev_speaker = sp
        lines.append(_format_segment(seg, show_speaker))
    return "\n".join(lines) + "\n"
