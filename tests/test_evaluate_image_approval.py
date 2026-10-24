"""Offline evaluation uses human truth and separates PDFs between splits."""

import json
from pathlib import Path

import pytest

from tools.evaluate_image_approval import DatasetError, evaluate, load_cases, main, release_gate


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "image_approval_eval_synthetic.jsonl"


def _write_cases(path, cases):
    path.write_text("\n".join(json.dumps(case) for case in cases) + "\n", encoding="utf-8")


def _case(case_id, document_id, split="development", **changes):
    return {"case_id": case_id, "document_id": document_id, "split": split,
            "category": "single_diagram", "human_label": "correct", "severe_error": False,
            "decision": "auto_pass", "current": True, **changes}


def test_synthetic_example_has_expected_denominators_and_never_claims_release():
    report = evaluate(load_cases(EXAMPLE))
    assert report["overall"]["cases"] == 7
    assert report["overall"]["auto_pass"] == 3
    assert report["overall"]["false_auto_pass"] == 1
    assert report["overall"]["severe_false_auto_pass"] == 0
    holdout = report["by_split"]["holdout"]
    assert holdout["auto_pass"] == 2
    assert holdout["false_auto_pass_rate"] == 0.5
    assert holdout["wrong_case_escape_rate"] == 0.5
    assert report["by_category"]["multi_figure"]["severe_false_auto_pass"] == 0
    assert report["by_split_category"]["holdout"]["single_diagram"]["false_auto_pass"] == 1
    assert "multi_figure" not in report["by_split_category"]["holdout"]
    assert release_gate(report)["passed"] is False


def test_no_auto_pass_reports_undefined_error_rate_instead_of_zero(tmp_path):
    path = tmp_path / "cases.jsonl"
    _write_cases(path, [_case("one", "pdf-A", decision="needs_review", human_label="wrong")])
    metrics = evaluate(load_cases(path))["overall"]
    assert metrics["auto_pass_coverage"] == 0
    assert metrics["false_auto_pass_rate"] is None
    assert metrics["wrong_case_escape_rate"] == 0


@pytest.mark.parametrize("changes,problem", [
    ({"decision": "pass"}, "decision"),
    ({"human_label": "uncertain"}, "human_label"),
    ({"severe_error": "false"}, "severe_error"),
    ({"severe_error": True}, "severe_error"),
    ({"case_id": ""}, "case_id"),
    ({"current": False}, "stale auto_pass"),
])
def test_invalid_rows_are_rejected(tmp_path, changes, problem):
    path = tmp_path / "cases.jsonl"
    case = _case("one", "pdf-A")
    case.update(changes)
    _write_cases(path, [case])
    with pytest.raises(DatasetError, match=problem):
        load_cases(path)


def test_duplicate_case_and_pdf_leakage_are_rejected(tmp_path):
    path = tmp_path / "cases.jsonl"
    first = _case("one", "pdf-A")
    _write_cases(path, [first, _case("one", "pdf-B")])
    with pytest.raises(DatasetError, match="duplicate case_id"):
        load_cases(path)
    _write_cases(path, [first, _case("two", "pdf-A", split="holdout")])
    with pytest.raises(DatasetError, match="both splits"):
        load_cases(path)


def test_unchecked_and_local_review_are_in_coverage_denominator(tmp_path):
    path = tmp_path / "cases.jsonl"
    _write_cases(path, [_case("auto", "pdf-A"),
                        _case("pending", "pdf-A", decision="unchecked", current=False),
                        _case("local-risk", "pdf-A", decision="needs_review", current=False)])
    metrics = evaluate(load_cases(path))["overall"]
    assert metrics["cases"] == 3
    assert metrics["auto_pass"] == 1
    assert metrics["auto_pass_coverage"] == 1 / 3


def test_proposed_gate_needs_document_diversity_and_detects_severe_false_pass(tmp_path):
    path = tmp_path / "cases.jsonl"
    development = [_case(f"dev-{i}", "dev-pdf") for i in range(50)]
    holdout = [_case(f"hold-{i}", f"held-pdf-{i % 3}", split="holdout") for i in range(150)]
    wrong = [_case(f"wrong-{i}", f"held-pdf-{i % 3}", split="holdout",
                   human_label="wrong", decision="attention") for i in range(30)]
    _write_cases(path, development + holdout + wrong)
    assert release_gate(evaluate(load_cases(path)))["passed"] is True

    holdout[0]["human_label"] = "wrong"
    holdout[0]["severe_error"] = True
    _write_cases(path, development + holdout + wrong)
    gate = release_gate(evaluate(load_cases(path)))
    assert gate["passed"] is False
    assert any("severe" in reason for reason in gate["reasons"])

    holdout[0]["human_label"] = "correct"
    holdout[0]["severe_error"] = False
    for item in holdout:
        item["document_id"] = "held-pdf-only"
    for item in wrong:
        item["document_id"] = "held-pdf-only"
    _write_cases(path, development + holdout + wrong)
    gate = release_gate(evaluate(load_cases(path)))
    assert gate["passed"] is False
    assert any("3 distinct PDFs" in reason for reason in gate["reasons"])


def test_gate_cannot_pass_only_positive_examples(tmp_path):
    path = tmp_path / "cases.jsonl"
    cases = [_case(f"dev-{i}", "dev-pdf") for i in range(50)]
    cases += [_case(f"hold-{i}", f"held-pdf-{i % 3}", split="holdout") for i in range(150)]
    _write_cases(path, cases)
    gate = release_gate(evaluate(load_cases(path)))
    assert gate["passed"] is False
    assert any("wrong-image cases" in reason for reason in gate["reasons"])


def test_cli_json_and_gate_exit_status(capsys):
    assert main([str(EXAMPLE), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["overall"]["false_auto_pass"] == 1
    assert main([str(EXAMPLE), "--check-release", "--json"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["release_gate"]["passed"] is False
