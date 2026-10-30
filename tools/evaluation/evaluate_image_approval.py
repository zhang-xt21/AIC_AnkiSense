"""Evaluate image auto-approval against human labels in a local JSONL file.

This tool does not call the model, inspect images, or change AnkiSense data.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import sys


SPLITS = {"development", "holdout"}
DECISIONS = {"auto_pass", "needs_review", "attention", "unchecked"}
HUMAN_LABELS = {"correct", "wrong"}


class DatasetError(ValueError):
    """The evaluation file is malformed or mixes development and holdout PDFs."""


@dataclass(frozen=True)
class Case:
    case_id: str
    document_id: str
    split: str
    category: str
    human_label: str
    severe_error: bool
    decision: str
    current: bool


def _nonempty_string(record: dict, key: str, line: int) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 160 or "\n" in value:
        raise DatasetError(f"line {line}: {key} must be a nonempty string of at most 160 characters")
    return value.strip()


def _case(record: object, line: int) -> Case:
    if not isinstance(record, dict):
        raise DatasetError(f"line {line}: expected one JSON object")
    values = {key: _nonempty_string(record, key, line) for key in
              ("case_id", "document_id", "split", "category", "human_label", "decision")}
    if values["split"] not in SPLITS:
        raise DatasetError(f"line {line}: split must be development or holdout")
    if values["human_label"] not in HUMAN_LABELS:
        raise DatasetError(f"line {line}: human_label must be correct or wrong")
    if values["decision"] not in DECISIONS:
        raise DatasetError(f"line {line}: decision must be one of {', '.join(sorted(DECISIONS))}")
    severe = record.get("severe_error")
    if type(severe) is not bool:
        raise DatasetError(f"line {line}: severe_error must be true or false")
    if severe and values["human_label"] != "wrong":
        raise DatasetError(f"line {line}: a severe_error must have human_label=wrong")
    current = record.get("current")
    if type(current) is not bool:
        raise DatasetError(f"line {line}: current must be true or false")
    if values["decision"] == "auto_pass" and not current:
        raise DatasetError(f"line {line}: a stale auto_pass cannot enter evaluation")
    return Case(**values, severe_error=severe, current=current)


def load_cases(path: Path) -> list[Case]:
    cases: list[Case] = []
    seen_ids: set[str] = set()
    document_splits: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError as error:
                    raise DatasetError(f"line {line_number}: invalid JSON ({error.msg})") from None
                case = _case(parsed, line_number)
                if case.case_id in seen_ids:
                    raise DatasetError(f"line {line_number}: duplicate case_id {case.case_id!r}")
                previous = document_splits.setdefault(case.document_id, case.split)
                if previous != case.split:
                    raise DatasetError(f"line {line_number}: document_id {case.document_id!r} appears in both splits")
                seen_ids.add(case.case_id)
                cases.append(case)
    except OSError as error:
        raise DatasetError(f"cannot read evaluation file: {error.strerror or error}") from error
    if not cases:
        raise DatasetError("evaluation file contains no cases")
    return cases


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _metrics(cases: list[Case]) -> dict:
    total = len(cases)
    auto = [case for case in cases if case.decision == "auto_pass"]
    false_auto = [case for case in auto if case.human_label == "wrong"]
    severe_auto = [case for case in false_auto if case.severe_error]
    wrong = [case for case in cases if case.human_label == "wrong"]
    return {
        "cases": total,
        "documents": len({case.document_id for case in cases}),
        "correct_cases": total - len(wrong),
        "wrong_cases": len(wrong),
        "auto_pass": len(auto),
        "auto_pass_documents": len({case.document_id for case in auto}),
        "auto_pass_coverage": _ratio(len(auto), total),
        "false_auto_pass": len(false_auto),
        "false_auto_pass_rate": _ratio(len(false_auto), len(auto)),
        "wrong_case_escape_rate": _ratio(len(false_auto), len(wrong)),
        "severe_false_auto_pass": len(severe_auto),
        "needs_review": sum(case.decision == "needs_review" for case in cases),
        "attention": sum(case.decision == "attention" for case in cases),
        "unchecked": sum(case.decision == "unchecked" for case in cases),
    }


def evaluate(cases: list[Case]) -> dict:
    """Report each decision against human truth, never against the model verdict."""
    grouped: dict[str, dict[str, list[Case]]] = {
        "by_split": defaultdict(list),
        "by_category": defaultdict(list),
        "by_document": defaultdict(list),
    }
    split_categories: dict[str, dict[str, list[Case]]] = {split: defaultdict(list) for split in SPLITS}
    for case in cases:
        grouped["by_split"][case.split].append(case)
        grouped["by_category"][case.category].append(case)
        grouped["by_document"][case.document_id].append(case)
        split_categories[case.split][case.category].append(case)
    return {
        "overall": _metrics(cases),
        "by_split": {key: _metrics(grouped["by_split"].get(key, [])) for key in sorted(SPLITS)},
        "by_category": {key: _metrics(group) for key, group in sorted(grouped["by_category"].items())},
        "by_split_category": {split: {key: _metrics(group) for key, group in sorted(categories.items())}
                              for split, categories in sorted(split_categories.items())},
        "by_document": {key: _metrics(group) for key, group in sorted(grouped["by_document"].items())},
    }


def release_gate(report: dict, *, max_false_auto_pass_rate: float = 0.01) -> dict:
    """Proposed pilot gate; a passing sample never guarantees future accuracy."""
    if not 0 <= max_false_auto_pass_rate <= 1:
        raise ValueError("max_false_auto_pass_rate must be between 0 and 1")
    development = report["by_split"]["development"]
    holdout = report["by_split"]["holdout"]
    reasons = []
    if development["cases"] < 50:
        reasons.append("development needs at least 50 human-labeled cases")
    if holdout["auto_pass"] < 150:
        reasons.append("holdout needs at least 150 auto_pass decisions")
    if holdout["auto_pass_documents"] < 3:
        reasons.append("holdout auto_pass cases must cover at least 3 distinct PDFs")
    if holdout["wrong_cases"] < 30:
        reasons.append("holdout needs at least 30 human-labeled wrong-image cases")
    if holdout["severe_false_auto_pass"]:
        reasons.append("holdout contains a severe false auto_pass")
    rate = holdout["false_auto_pass_rate"]
    if rate is None or rate > max_false_auto_pass_rate:
        reasons.append(f"holdout false auto_pass rate must be <= {max_false_auto_pass_rate:.1%}")
    return {"passed": not reasons, "reasons": reasons,
            "note": "A passing offline sample does not guarantee image correctness on new PDFs."}


def _percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1%}"


def format_report(report: dict, gate: dict | None = None) -> str:
    lines = ["配图自动审批离线验收（人工标注为准）"]
    for name, data in [("全部", report["overall"]),
                       ("开发集", report["by_split"]["development"]),
                       ("留出集", report["by_split"]["holdout"])]:
        lines.append(f"{name}: {data['cases']} 例 / {data['documents']} 份 PDF；"
                     f"自动通过 {data['auto_pass']} 例（覆盖率 {_percent(data['auto_pass_coverage'])}）；"
                     f"错放 {data['false_auto_pass']} 例 / 自动通过（{_percent(data['false_auto_pass_rate'])}）；"
                     f"严重错放 {data['severe_false_auto_pass']} 例")
    lines.append("类别：")
    for category, data in report["by_category"].items():
        lines.append(f"  {category}: {data['cases']} 例，自动通过 {data['auto_pass']}，"
                     f"错放 {data['false_auto_pass']}，严重错放 {data['severe_false_auto_pass']}")
    lines.append("留出集类别：")
    for category, data in report["by_split_category"]["holdout"].items():
        lines.append(f"  {category}: {data['cases']} 例，自动通过 {data['auto_pass']}，"
                     f"错放 {data['false_auto_pass']}，严重错放 {data['severe_false_auto_pass']}")
    lines.append("文档：")
    for document_id, data in report["by_document"].items():
        lines.append(f"  {document_id}: {data['cases']} 例，自动通过 {data['auto_pass']}，"
                     f"错放 {data['false_auto_pass']}")
    if gate is not None:
        lines.append("试点门槛：" + ("通过" if gate["passed"] else "未通过"))
        lines.extend(f"  - {reason}" for reason in gate["reasons"])
        lines.append(gate["note"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path, help="human-labeled local JSONL dataset")
    parser.add_argument("--json", action="store_true", help="print machine-readable report")
    parser.add_argument("--check-release", action="store_true", help="apply the proposed pilot threshold")
    parser.add_argument("--max-false-auto-pass-rate", type=float, default=0.01,
                        help="allowed holdout error rate among auto_pass cases (default: 0.01)")
    args = parser.parse_args(argv)
    if not 0 <= args.max_false_auto_pass_rate <= 1:
        parser.error("--max-false-auto-pass-rate must be between 0 and 1")
    try:
        report = evaluate(load_cases(args.jsonl))
        gate = release_gate(report, max_false_auto_pass_rate=args.max_false_auto_pass_rate) if args.check_release else None
    except DatasetError as error:
        print(f"Invalid evaluation dataset: {error}", file=sys.stderr)
        return 2
    payload = {**report, "release_gate": gate} if gate is not None else report
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else format_report(report, gate))
    return 0 if gate is None or gate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
