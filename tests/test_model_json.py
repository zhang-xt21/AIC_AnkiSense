import asyncio
import json
from pathlib import Path
import re

import httpx
import pytest

from ankisense.model_json import load_model_json
from test_llm import cloud_service


@pytest.mark.parametrize("number", [105, 106, 108, 109])
def test_recorded_failed_responses_recover_without_changing_source_quotes(number):
    content = (Path(__file__).parent / "fixtures/invalid_math_json" / f"request-{number}.txt").read_text(encoding="utf-8")
    with pytest.raises(json.JSONDecodeError, match="Invalid"):
        json.loads(content)
    value = load_model_json(content)
    assert len(value["cards"]) == 3
    quote_tokens = re.findall(r'"quote"\s*:\s*("(?:[^"\\]|\\.)*")', content)
    expected = [json.loads(token) for token in quote_tokens]
    assert expected
    assert [e["quote"] for item in value["cards"] for e in item["card"]["evidence"]] == expected
    assert all("\\(" in item["card"]["question"] for item in value["cards"])


def test_valid_json_and_literal_protected_text_keep_their_values():
    value = {"question": r"设 \(x\in V\)，x是什么？", "evidence": [{"quote": r"原文\(x\)，C:\new\file.txt"}],
             "answer_spans": [r"学生原文\(x\)"], "code": "if (x) { return 1; }"}
    assert load_model_json(json.dumps(value, ensure_ascii=False)) == value


def test_fallback_is_limited_to_display_fields_and_keeps_valid_json_escapes():
    text = r'{"question":"设 \(x\)，求值。","answer":"\[x=1\]","rubric":[{"text":"写出 \(1\)"}],"evidence":[{"quote":"原文\\(x\\)\\n"}],"path":"C:\\new\\file.txt"}'
    result = load_model_json(text)
    assert result["question"] == r"设 \(x\)，求值。"
    assert result["rubric"][0]["text"] == r"写出 \(1\)"
    assert result["evidence"][0]["quote"] == r"原文\(x\)\n"
    assert result["path"] == r"C:\new\file.txt"


@pytest.mark.parametrize("text", [
    r'{"question":"\(x\)","evidence":[{"quote":"\(raw\)"}]}',
    r'{"question":"\(x\)","answer_spans":["\(student\)"]}',
    r'{"question":"\(x\)","exact_quote":"\(raw\)"}',
    r'{"question":"\(\alpha\)"}',  # Unknown command escapes are never guessed.
    r'{"question":"\(x"}',
    r'{"question":"\(x\]"}',
    r'{"question":"\(x\)"',
    r'{"question":"\(x\)",}',
    r'{"other":"\(x\)"}',
    '```json\n{"question":"x"}\n```',
])
def test_other_damage_and_protected_fields_still_fail(text):
    with pytest.raises((ValueError, json.JSONDecodeError)):
        load_model_json(text)


def test_transport_recovers_once_without_an_extra_model_request(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": r'{"answer":"\(x=1\)"}'}}]})
    ai = cloud_service(monkeypatch, handler)
    result = asyncio.run(ai._request("offline fixture", {}, {"type": "object"}))
    assert result == {"answer": r"\(x=1\)"}
    assert len(calls) == 1
