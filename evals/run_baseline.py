"""Generate or resume the offline quality-suite baseline with the current pipeline."""
from __future__ import annotations

import hashlib
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "object"))

from contracts import Options
from evaluation import evaluate, validate_case
from service import Service
from storage import Store


CASES = ROOT / "evals" / "cases"
DATA = ROOT / "evals" / "baseline-data"
SEEDS = ROOT / "evals" / "baseline-seeds.json"
MANIFEST = ROOT / "work" / "eval-baseline-manifest.json"
REPORT = ROOT / "work" / "eval-suite-baseline.json"


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def register(store, source):
    ext = source.suffix.lower()
    with source.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    file_id = hashlib.sha256((sha + ext).encode()).hexdigest()
    target = store.root / "uploads" / (file_id + ext)
    try:
        store.file(file_id)
    except KeyError:
        shutil.copy2(source, target)
        store.register_file(file_id, source.name, target, sha, source.stat().st_size)
    return file_id


def options_for(case):
    kind = case["material_type"]
    return Options(
        template="meeting" if kind == "meeting_audio" else "course",
        detail="normal",
        include_images=True,
        diarization=case["id"] == "meeting-plan-audio",
        visual_mode="auto",
    )


def summarize(reports):
    score_keys = ("required_claim_recall", "forbidden_pair_precision", "visual_binding_recall")
    def aggregate(rows):
        costs = [report["cost"] for report in rows]
        return {
            "case_count": len(rows),
            "average_scores": {
                key: sum(report["scores"][key] for report in rows) / len(rows)
                for key in score_keys
            },
            "unresolved_issue_count": sum(report["scores"]["unresolved_issue_count"] for report in rows),
            "cost": {
                "seconds": sum(item.get("seconds") or 0 for item in costs),
                "model_calls": sum(item.get("model_calls") or 0 for item in costs),
                "input_tokens": sum(item.get("input_tokens") or 0 for item in costs),
                "output_tokens": sum(item.get("output_tokens") or 0 for item in costs),
            },
        }
    summary = aggregate(reports)
    summary["by_material_type"] = {
        kind: aggregate([report for report in reports if report["material_type"] == kind])
        for kind in sorted({report["material_type"] for report in reports})
    }
    return summary


def main():
    parser=argparse.ArgumentParser(description='Generate or resume a Nota quality-suite run')
    parser.add_argument('--run-name',default='baseline')
    parser.add_argument('--no-seeds',action='store_true')
    args=parser.parse_args()
    data_path=DATA if args.run_name=='baseline' else ROOT/'evals'/(args.run_name+'-data')
    manifest_path=MANIFEST if args.run_name=='baseline' else ROOT/'work'/('eval-'+args.run_name+'-manifest.json')
    report_path=REPORT if args.run_name=='baseline' else ROOT/'work'/('eval-'+args.run_name+'.json')
    cases = [validate_case(read_json(path, {})) for path in sorted(CASES.glob("*.json"))]
    seeds = {} if args.no_seeds else read_json(SEEDS, {})
    manifest = read_json(manifest_path, {})
    eval_store = Store(data_path)
    service = Service(eval_store, workers=1)
    reports = []
    try:
        for index, case in enumerate(cases, 1):
            case_id = case["id"]
            print(f"[{index}/{len(cases)}] {case_id}", flush=True)
            location = manifest.get(case_id) or seeds.get(case_id)
            if location:
                store = Store(ROOT / location["data_root"])
                note = store.note(location["note_id"], location.get("version"))
            else:
                source = ROOT / case["source_path"]
                file_id = register(eval_store, source)
                payload = {"file_ids": [file_id], "options": options_for(case).model_dump()}
                job_id = eval_store.new_job("generate", payload)
                service.run(job_id)
                job = eval_store.job(job_id)
                if job["status"] not in ("succeeded", "partial") or not job.get("result"):
                    raise RuntimeError(f"{case_id} failed: {job.get('error')}")
                location = {"data_root": "evals/baseline-data", "note_id": job["result"]["note_id"], "version": 1}
                location['data_root']=str(data_path.relative_to(ROOT)).replace('\\','/')
                manifest[case_id] = location
                write_json(manifest_path, manifest)
                note = eval_store.note(location["note_id"], 1)
            report = evaluate(note, case)
            report["material_type"] = case["material_type"]
            report["synthetic"] = case["synthetic"]
            reports.append(report)
            write_json(report_path, {"summary": summarize(reports), "cases": reports})
    finally:
        service.pool.shutdown(wait=True)
    print(json.dumps(summarize(reports), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
