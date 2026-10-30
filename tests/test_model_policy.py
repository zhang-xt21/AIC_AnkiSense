"""Fast-mode transport checks use local HTTP doubles, never a model API."""

import asyncio
import json

import httpx
import pytest

from ankisense.demo import seed_records
from ankisense.llm import AIError, AIService, Settings
from ankisense.model_policy import generation_options
from ankisense.schemas import CardDraft, SourceBlock
from ankisense.figure_vision import generation_params


@pytest.mark.parametrize("endpoint,model,disabled", [
    ("https://api.deepseek.com", "deepseek-flash", True),
    ("https://api.deepseek.com/v1", "deepseek-v4-pro", True),
    ("https://api.deepseek.com:443/v1", "deepseek-flash", True),
    ("https://api.deepseek.com:444/v1", "deepseek-flash", False),
    ("https://api.deepseek.com", "another-model", False),
    ("https://api.deepseek.com.example/v1", "deepseek-flash", False),
    ("https://proxy.example/v1", "deepseek-flash", False),
    ("https://api.openai.com/v1", "example-model", False),
    ("http://127.0.0.1:8123/v1", "deepseek-flash", False),
    ("https://api.deepseek.com:bad-port/v1", "deepseek-flash", False),
])
def test_provider_options_match_text_and_isolated_vision_worker(endpoint, model, disabled):
    options = generation_options(endpoint, model)
    assert options == ({"thinking": {"type": "disabled"}} if disabled else {})
    params = generation_params(endpoint, model)
    assert params.get("thinking") == options.get("thinking")
    assert params["max_tokens"] == 4096


@pytest.mark.parametrize("task", ["generate", "review", "diagnose", "proofread"])
@pytest.mark.parametrize("provider", ["deepseek", "compatible"])
def test_all_model_tasks_disable_deepseek_thinking_without_affecting_compatible_apis(monkeypatch, task, provider):
    records = seed_records()
    record = next(record for table, record in records if table == "cards" and record["id"] == "demo-stack")
    card = CardDraft.model_validate({key: record[key] for key in CardDraft.model_fields})
    blocks = [SourceBlock.model_validate(record) for table, record in records if table == "blocks"]
    citation = {"block_id": card.evidence[0].block_id, "exact_quote": card.evidence[0].quote}
    review = {"learning_scope": {"verdict": "single", "targets": [{"question_span": card.question, "goal": "栈操作"}], "reason": "同一核心目标。"}, "minimal_answer": card.answer, "minimal_answer_citations": [citation], "decision": "pass", "question_answerable": True, "answer_relevant": True,
              "answer_supported": True, "unsupported_claims": [], "answer_citations": [citation],
              "question_coverage": "complete", "missing_question_parts": [],
              "rubric_checks": [{"point_id": point.point_id, "verdict": "supported", "scope": "required", "scope_reason": "回答所问。", **citation}
                                for point in card.rubric], "issues": []}
    responses = {
        "generate": {"cards": [card.model_dump()]},
        "review": review,
        "diagnose": {"verdict": "correct", "missing_point_ids": [], "contradicted_point_ids": [],
                     "answer_spans": ["后进先出"], "source_block_ids": ["demo-block-1"], "reason": "符合引用。"},
        "proofread": {"suggested_text": blocks[0].text, "issues": [], "uncertain": False},
    }
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload.get("thinking") == ({"type": "disabled"} if provider == "deepseek" else None)
        assert "reasoning_effort" not in payload
        if task == "proofread":
            assert payload["messages"][1]["content"][1]["type"] == "image_url"
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(responses[task], ensure_ascii=False)}}]})

    client_class = httpx.AsyncClient
    monkeypatch.setattr("ankisense.llm.httpx.AsyncClient",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs))
    service = AIService(Settings(mode="cloud", api_key="test-only", model="deepseek-flash",
                                 base_url="https://api.deepseek.com" if provider == "deepseek" else "https://proxy.example/v1"))

    async def run():
        if task == "generate":
            return await service.generate_drafts(blocks, 1)
        if task == "review":
            return await service.review_card(card, blocks)
        if task == "diagnose":
            return await service.diagnose_answer(card, blocks, "后进先出")
        return await service.proofread_page(b"\x89PNG\r\n\x1a\nfixture", blocks[0].text, 1)

    assert asyncio.run(run()) is not None
    assert len(calls) == 1


def test_rejected_fast_mode_is_not_retried_with_provider_defaults(monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(400)

    client_class = httpx.AsyncClient
    monkeypatch.setattr("ankisense.llm.httpx.AsyncClient",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs))
    service = AIService(Settings(mode="cloud", base_url="https://api.deepseek.com", model="deepseek-flash", api_key="test-only"))
    with pytest.raises(AIError):
        asyncio.run(service._request("Test", {}, {}))
    assert len(calls) == 1 and calls[0]["thinking"] == {"type": "disabled"}
