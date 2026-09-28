"""Deterministic, offline quality checks for generated Nota notes."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from storage import Store


def validate_case(case: dict) -> dict:
    if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"].strip():
        raise ValueError("评测案例必须包含非空 id")
    if not isinstance(case.get("source_name"), str) or not case["source_name"].strip():
        raise ValueError("评测案例必须包含非空 source_name")
    if not isinstance(case.get("source_path"), str) or not case["source_path"].strip():
        raise ValueError("评测案例必须包含非空 source_path")
    allowed_types = {"paper", "scanned_pdf", "lecture_slides", "docx", "meeting_audio"}
    if case.get("material_type") not in allowed_types:
        raise ValueError("评测案例 material_type 无效")
    if not isinstance(case.get("synthetic"), bool):
        raise ValueError("评测案例 synthetic 必须是布尔值")
    for key in ("required_claims", "forbidden_pairs", "visual_expectations", "allowed_omissions"):
        if key in case and not isinstance(case[key], list):
            raise ValueError(f"{key} 必须是列表")
    ids = []
    for item in case.get("required_claims", []):
        if not isinstance(item.get("all"), list) or not all(isinstance(x, str) for x in item["all"]):
            raise ValueError("required_claims.all 必须是正则表达式列表")
        ids.append(item.get("id"))
    for item in case.get("forbidden_pairs", []):
        if not all(isinstance(item.get(key), str) for key in ("id", "left", "right")):
            raise ValueError("forbidden_pairs 必须包含 id、left 和 right")
        if not isinstance(item.get("allow_if", []), list) or not all(isinstance(x, str) for x in item.get("allow_if", [])):
            raise ValueError("forbidden_pairs.allow_if 必须是正则表达式列表")
        ids.append(item["id"])
    for item in case.get("visual_expectations", []):
        if not isinstance(item.get("source_any"), list) or not isinstance(item.get("chapter_any"), list):
            raise ValueError("visual_expectations 必须包含 source_any 和 chapter_any 列表")
        ids.append(item.get("id"))
    if any(not isinstance(item, str) or not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("评测规则 id 必须非空且不能重复")
    try:
        for item in case.get("required_claims", []):
            for pattern in item.get("all", []) + item.get("any", []):
                re.compile(pattern)
        for item in case.get("forbidden_pairs", []):
            re.compile(item["left"]); re.compile(item["right"])
            for pattern in item.get("allow_if", []):re.compile(pattern)
        for item in case.get("visual_expectations", []):
            for pattern in item["source_any"] + item["chapter_any"]:
                re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"评测案例包含无效正则表达式：{exc}") from None
    return case


def _matches(pattern: str, text: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) is not None


def _chapter_text(chapter: dict) -> str:
    return f"{chapter.get('title', '')}\n{chapter.get('markdown', '')}"


def evaluate(note: dict, case: dict) -> dict:
    case = validate_case(case)
    data = note["data"] if "data" in note else note
    chapters = data.get("chapters", [])
    sources = data.get("sources", [])
    full_text = "\n\n".join(_chapter_text(chapter) for chapter in chapters)

    claim_results = []
    for claim in case.get("required_claims", []):
        all_hits = {pattern: _matches(pattern, full_text) for pattern in claim.get("all", [])}
        any_patterns = claim.get("any", [])
        any_hits = {pattern: _matches(pattern, full_text) for pattern in any_patterns}
        passed = all(all_hits.values()) and (not any_patterns or any(any_hits.values()))
        claim_results.append({"id": claim["id"], "passed": passed, "all": all_hits, "any": any_hits})

    pair_results = []
    for rule in case.get("forbidden_pairs", []):
        left = list(re.finditer(rule["left"], full_text, flags=re.IGNORECASE))
        right = list(re.finditer(rule["right"], full_text, flags=re.IGNORECASE))
        window = int(rule.get("window", 120))
        violations = []
        for a in left:
            for b in right:
                if abs(a.start() - b.start()) <= window:
                    start = max(0, min(a.start(), b.start()) - 40)
                    end = min(len(full_text), max(a.end(), b.end()) + 40)
                    context=full_text[start:end].replace("\n", " ")
                    if not any(_matches(pattern,context) for pattern in rule.get("allow_if", [])):
                        violations.append(context)
        pair_results.append({"id": rule["id"], "passed": not violations, "violations": violations[:5]})

    source_by_id = {source.get("id"): source for source in sources}
    visual_results = []
    for expectation in case.get("visual_expectations", []):
        bindings = []
        for chapter in chapters:
            chapter_text = _chapter_text(chapter)
            if not any(_matches(pattern, chapter_text) for pattern in expectation.get("chapter_any", [])):
                continue
            for source_id in chapter.get("source_ids", []):
                source = source_by_id.get(source_id, {})
                if not source.get("asset"):
                    continue
                source_text = source.get("text", "")
                if any(_matches(pattern, source_text) for pattern in expectation.get("source_any", [])):
                    bindings.append({"chapter_id": chapter.get("id"), "source_id": source_id, "asset": source["asset"]})
        visual_results.append({"id": expectation["id"], "passed": bool(bindings), "bindings": bindings})

    claim_passed = sum(item["passed"] for item in claim_results)
    pair_passed = sum(item["passed"] for item in pair_results)
    visual_passed = sum(item["passed"] for item in visual_results)
    issue_count = len(data.get("quality", {}).get("issues", []))
    metrics = data.get("metrics_summary", {})
    return {
        "case_id": case["id"],
        "note_id": note.get("id"),
        "note_version": note.get("version"),
        "scores": {
            "required_claim_recall": claim_passed / len(claim_results) if claim_results else 1.0,
            "forbidden_pair_precision": pair_passed / len(pair_results) if pair_results else 1.0,
            "visual_binding_recall": visual_passed / len(visual_results) if visual_results else 1.0,
            "unresolved_issue_count": issue_count,
        },
        "cost": {
            "seconds": metrics.get("seconds"),
            "model_calls": metrics.get("model_calls"),
            "input_tokens": metrics.get("input_tokens"),
            "output_tokens": metrics.get("output_tokens"),
        },
        "pipeline": {
            "version": data.get("pipeline_version"),
            "models": data.get("models", {}),
            "options": data.get("options", {}),
        },
        "required_claims": claim_results,
        "forbidden_pairs": pair_results,
        "visual_expectations": visual_results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a saved Nota note without online model calls")
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--note-id", required=True)
    parser.add_argument("--version", type=int)
    parser.add_argument("--data-root", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    case = validate_case(json.loads(args.case.read_text(encoding="utf-8")))
    note = Store(args.data_root).note(args.note_id, args.version)
    report = evaluate(note, case)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.buffer.write((rendered + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
