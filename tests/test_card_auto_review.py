"""Whole-card review approves only unchanged, fully supported current cards.

All model and image calls in this module are local doubles.  The PDF fixture is a
real, reviewed source page, so these tests exercise the public API boundary.
"""

import asyncio
import copy
import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from PIL import Image

from ankisense.llm import AIError, Settings
from ankisense.schemas import Diagnosis
from ankisense.sources import save_page_revision
from test_figure_recommendations import context, save
from test_image_auto_review import saved as saved_image, post as check_image


def route(card):
    return f"/api/cards/{card['id']}/auto-review"


def post(client, card, version=None):
    return client.post(route(card), json={"expected_version": card["version"] if version is None else version})


def good_review(card):
    evidence = card["evidence"][0]
    citation = {"block_id": evidence["block_id"], "exact_quote": evidence["quote"]}
    return {
        "learning_scope": {"verdict": "single", "targets": [{"question_span": card["question"], "goal": "一个完整的复习目标"}], "reason": "题干围绕同一个核心知识目标。"},
        "minimal_answer": card["answer"], "minimal_answer_citations": [citation],
        "decision": "pass",
        "question_answerable": True,
        "answer_relevant": True,
        "answer_supported": True,
        "question_coverage": "complete", "missing_question_parts": [],
        "unsupported_claims": [],
        "answer_citations": [citation],
        "rubric_checks": [
            {"point_id": point["point_id"], "verdict": "supported", "scope": "required", "scope_reason": "回答题目所问。", **citation}
            for point in card["rubric"]
        ],
        "issues": [],
    }


@pytest.mark.parametrize("scope", ["given", "background", "uncertain"])
def test_supported_fact_cannot_pass_when_it_is_not_a_confirmed_question_requirement(ready, monkeypatch, scope):
    client, card, service, calls = ready
    review = good_review(card)
    review["rubric_checks"][0].update(scope=scope, scope_reason="题目未明确要求此背景。")
    # Even an inconsistent model pass cannot bypass the server's scope gate.
    async def result(*_):
        return review
    monkeypatch.setattr(client.app.state.ai, "review_card", result)
    response = post(client, card).json()
    assert response["status"] != "auto_pass"
    assert service.store.get("cards", card["id"])["status"] == "draft"


def test_background_requirement_is_reported_as_actionable_attention(ready, monkeypatch):
    client, card, service, calls = ready
    review = good_review(card)
    review.update(decision="revise", issues=["此评分条件只是背景，应从必答要点中移除。"])
    review["rubric_checks"][0].update(scope="background", scope_reason="学生可以正确回答所问而省略此项。")
    async def result(*_):
        return review
    monkeypatch.setattr(client.app.state.ai, "review_card", result)
    response = post(client, card).json()
    assert response["status"] == "attention"
    assert response["checks"]["rubric_checks"][0]["verdict"] == "supported"
    assert service.store.get("cards", card["id"])["status"] == "draft"


def test_review_must_explicitly_check_all_question_parts_and_reject_legacy_format(ready, monkeypatch):
    client, card, service, calls = ready
    review = good_review(card)
    review.pop("question_coverage")
    async def result(*_):
        return review
    monkeypatch.setattr(client.app.state.ai, "review_card", result)
    assert post(client, card).json()["status"] == "needs_review"
    assert service.store.get("cards", card["id"])["status"] == "draft"


def test_old_machine_policy_cannot_appear_learnable_or_overwrite_pending_ui_state(ready):
    client, card, service, calls = ready
    result = post(client, card).json()
    record = service.store.get("card_auto_reviews", result["review_id"])
    record["policy_version"] = "whole-card-review-v2"
    service.store.put("card_auto_reviews", record)
    projected = next(item for item in client.get("/api/state").json()["cards"] if item["id"] == card["id"])
    assert projected["status"] == "draft" and projected["approval_stale"] is True
    review_response = client.get(route(card)).json()
    assert review_response["card"]["status"] == "draft" and review_response["card_status"] == "draft"
    assert service.store.get("cards", card["id"])["status"] == "approved", "Reading must not mutate history"


def bind_imported_pdf(client, card, storage):
    """Give the synthetic document the same immutable PDF binding as an import."""
    store = client.app.state.store
    document = store.get("documents", card["document_id"])
    pdf = (storage / "uploads" / f"{card['document_id']}.pdf").read_bytes()
    document["pdf_sha256"] = hashlib.sha256(pdf).hexdigest()
    document["extraction_mode"] = "text"
    store.put("documents", document)


@pytest.fixture
def ready(context, monkeypatch):
    client, original, _adapter, storage = context
    bind_imported_pdf(client, original, storage)
    card_response = save(client, original, status="draft", images=[])
    assert card_response.status_code == 200, card_response.text
    card = card_response.json()
    service = client.app.state.card_auto_review
    service.settings = Settings(mode="cloud", base_url="https://model.invalid/v1",
                                model="fixture-reviewer", api_key="test-key")
    calls = []

    async def review(_card, _blocks):
        calls.append(True)
        return copy.deepcopy(good_review(card))

    monkeypatch.setattr(client.app.state.ai, "review_card", review)
    monkeypatch.setattr(client.app.state.ai, "diagnose_answer", correct_reference_diagnosis)
    monkeypatch.setattr(client.app.state.ai, "minimal_answer_for_question", minimal_answer_fixture)
    return client, card, service, calls


async def correct_reference_diagnosis(card, blocks, answer):
    return Diagnosis(verdict="correct", addressed_point_ids=[p.point_id for p in card.rubric],
                     missing_point_ids=[], contradicted_point_ids=[], answer_spans=[answer],
                     source_block_ids=[card.evidence[0].block_id], reason="离线自测固定响应。")


async def minimal_answer_fixture(card, blocks):
    return card.answer


def test_independent_minimal_answer_failure_blocks_saved_card_release(ready, monkeypatch):
    client, card, service, _ = ready
    async def minimal(draft, blocks):
        return "最短回答。"
    async def diagnose(draft, blocks, answer):
        if answer != "最短回答。":
            return await correct_reference_diagnosis(draft, blocks, answer)
        point = draft.rubric[0].point_id
        return Diagnosis(verdict="incomplete", addressed_point_ids=[point], missing_point_ids=[point],
                         contradicted_point_ids=[], answer_spans=[answer], source_block_ids=[draft.evidence[0].block_id],
                         reason="评分点多要求额外表示。")
    monkeypatch.setattr(client.app.state.ai, "minimal_answer_for_question", minimal)
    monkeypatch.setattr(client.app.state.ai, "diagnose_answer", diagnose)
    response = post(client, card).json()
    assert response["status"] == "attention"
    stored = service.store.get("card_auto_reviews", response["review_id"])
    assert stored["reference_answer_check"]["verdict"] == "correct"
    assert stored["minimal_answer_check"]["diagnosis"]["verdict"] == "incomplete"
    assert service.store.get("cards", card["id"])["status"] == "draft"


@pytest.fixture(autouse=True)
def reference_answer_fixture(context, monkeypatch):
    client, *_ = context
    monkeypatch.setattr(client.app.state.ai, "diagnose_answer", correct_reference_diagnosis)
    monkeypatch.setattr(client.app.state.ai, "minimal_answer_for_question", minimal_answer_fixture)


def test_reference_answer_failure_blocks_saved_card_machine_approval(ready, monkeypatch):
    client, card, service, _ = ready
    async def incomplete(draft, blocks, answer):
        point = draft.rubric[0].point_id
        return Diagnosis(verdict="incomplete", addressed_point_ids=[point], missing_point_ids=[point],
                         contradicted_point_ids=[], answer_spans=[answer],
                         source_block_ids=[draft.evidence[0].block_id], reason="参考答案未满足额外评分条件。")
    monkeypatch.setattr(client.app.state.ai, "diagnose_answer", incomplete)
    response = post(client, card).json()
    assert response["status"] == "attention"
    assert service.store.get("cards", card["id"])["status"] == "draft"
    stored = service.store.get("card_auto_reviews", response["review_id"])
    assert stored["reference_answer_check"]["verdict"] == "incomplete"


def test_reviewed_pdf_card_pass_atomically_approves_without_content_version_change(ready):
    client, card, service, calls = ready
    before = client.get(route(card))
    assert before.status_code == 200, before.text
    assert before.json()["status"] == "unchecked"
    assert before.json()["current"] is False
    assert calls == []

    response = post(client, card)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "auto_pass" and result["current"] is True
    assert result["cached"] is False and result["review_id"]
    assert isinstance(result["checks"], dict) and result["issues"] == []
    assert result["checked_at"] and len(calls) == 1

    again = post(client, card)
    assert again.status_code == 200, again.text
    assert again.json()["cached"] is True and len(calls) == 1
    assert client.get(route(card)).json()["status"] == "auto_pass"
    approved = service.store.get("cards", card["id"])
    assert approved["status"] == "approved" and approved["version"] == card["version"]
    assert approved["approval"] == {"method": "machine", "review_id": result["review_id"],
                                    "card_version": card["version"], "approved_at": result["checked_at"]}
    assert card["id"] in client.get("/api/export").text


def test_thinking_policy_change_invalidates_previous_whole_card_check(ready, monkeypatch):
    client, card, service, calls = ready
    service.settings = Settings(mode="cloud", base_url="https://api.deepseek.com", model="deepseek-flash", api_key="test-only")
    with monkeypatch.context() as previous_policy:
        previous_policy.setattr("ankisense.card_auto_review.generation_options", lambda *_args: {})
        previous = post(client, card).json()
    stale = client.get(route(card)).json()
    assert stale["stale"] is True and stale["current"] is False
    current = post(client, card).json()
    assert current["review_id"] != previous["review_id"] and current["cached"] is False
    assert len(calls) == 2
    assert post(client, card).json()["cached"] is True and len(calls) == 2


@pytest.mark.parametrize("change", [
    {"decision": "revise", "answer_supported": False,
     "unsupported_claims": ["参考答案把出栈顺序说反了。"], "issues": ["答案与原文矛盾。"]},
    {"decision": "uncertain", "answer_supported": False,
     "issues": ["讲义未给出足以核对答案的内容。"]},
])
def test_wrong_or_unsupported_answer_is_never_auto_pass(ready, monkeypatch, change):
    client, card, _service, calls = ready
    card = save(client, card, answer="先进先出。").json()

    async def review(_card, _blocks):
        calls.append(True)
        return {**good_review(card), **change}

    monkeypatch.setattr(client.app.state.ai, "review_card", review)
    response = post(client, card)
    assert response.status_code == 200, response.text
    assert response.json()["status"] in {"attention", "needs_review"}
    assert response.json()["status"] != "auto_pass"
    assert response.json()["reasons"] and len(calls) == 1
    assert client.app.state.store.get("cards", card["id"])["status"] == "draft"


@pytest.mark.parametrize("mutation", ["omitted_rubric", "fabricated_answer_quote", "fabricated_rubric_quote"])
def test_incomplete_or_fabricated_model_evidence_never_passes(ready, monkeypatch, mutation):
    client, card, _service, calls = ready
    result = good_review(card)
    if mutation == "omitted_rubric":
        result["rubric_checks"] = []
    elif mutation == "fabricated_answer_quote":
        result["answer_citations"][0]["exact_quote"] = "讲义并没有这句话。"
    else:
        result["rubric_checks"][0]["exact_quote"] = "讲义并没有这句话。"

    async def review(_card, _blocks):
        calls.append(True)
        return result

    monkeypatch.setattr(client.app.state.ai, "review_card", review)
    response = post(client, card)
    assert response.status_code == 200, response.text
    assert response.json()["status"] != "auto_pass"
    assert client.app.state.store.get("cards", card["id"])["status"] == "draft"
    assert len(calls) == 1


def test_outdated_card_version_rejected_before_model_call(ready):
    client, card, service, calls = ready
    response = post(client, card, version=card["version"] - 1)
    assert response.status_code == 409, response.text
    assert calls == [] and service.store.all("card_auto_reviews") == []


def test_source_revision_invalidates_pass_without_reusing_model_result(ready):
    client, card, service, calls = ready
    assert post(client, card).json()["status"] == "auto_pass"
    save_page_revision(service.store, card["document_id"], 2, 1,
                       {"text": "栈遵循后进先出，插入和删除在栈顶进行。已重新校对。",
                        "status": "reviewed", "reviewed_at": "now"}, "proofread")
    current = service.store.get("cards", card["id"])
    assert current["status"] == "draft" and current["source_outdated"] is True
    fetched = client.get(route(card))
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] != "auto_pass"
    assert fetched.json()["current"] is False and fetched.json()["stale"] is True
    assert len(calls) == 1


def test_pdf_bytes_change_invalidates_pass_and_blocks_new_model_call(ready):
    client, card, service, calls = ready
    assert post(client, card).json()["status"] == "auto_pass"
    path = service.storage / "uploads" / f"{card['document_id']}.pdf"
    path.write_bytes(path.read_bytes() + b"\n% altered after import")
    fetched = client.get(route(card))
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] != "auto_pass"
    assert fetched.json()["current"] is False and fetched.json()["stale"] is True
    checked = post(client, card)
    assert checked.status_code == 200, checked.text
    assert checked.json()["status"] != "auto_pass" and len(calls) == 1


@pytest.mark.parametrize("mutation", ["question", "model"])
def test_card_or_model_change_invalidates_prior_pass(ready, mutation):
    client, card, service, calls = ready
    assert post(client, card).json()["status"] == "auto_pass"
    if mutation == "question":
        changed = save(client, card, question="栈的出栈顺序是什么？")
        assert changed.status_code == 200, changed.text
    else:
        service.settings = Settings(mode="cloud", base_url="https://model.invalid/v1",
                                    model="new-reviewer", api_key="test-key")
    fetched = client.get(route(card))
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] != "auto_pass"
    assert fetched.json()["current"] is False and fetched.json()["stale"] is True
    assert len(calls) == 1


def test_unusable_source_page_cannot_be_auto_passed(ready):
    client, card, service, calls = ready
    page = service.store.get("source_pages", f"{card['document_id']}-p2")
    page["status"] = "extracted"
    service.store.put("source_pages", page)
    response = post(client, card)
    assert response.status_code == 200, response.text
    assert response.json()["status"] != "auto_pass"
    assert calls == []


def test_pdf_block_without_current_source_page_never_calls_model(ready):
    client, card, service, calls = ready
    block_id = f"{card['document_id']}-p4-b1-r1"
    service.store.put("blocks", {"block_id": block_id, "document_id": card["document_id"],
                                 "page": 4, "text": "栈遵循后进先出。"})
    changed = save(client, card, evidence=[{"block_id": block_id, "quote": "栈遵循后进先出"}])
    assert changed.status_code == 200, changed.text
    response = post(client, changed.json())
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needs_review"
    assert calls == []


def test_demo_card_never_calls_model_for_automatic_approval(ready):
    client, _card, _service, calls = ready
    seeded = client.post("/api/demo")
    assert seeded.status_code == 200, seeded.text
    demo_card = next(item for item in client.app.state.store.all("cards") if item["document_id"] == seeded.json()["document_id"])
    response = post(client, demo_card)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needs_review"
    assert calls == []


def test_pasted_text_card_never_calls_model_for_automatic_approval(ready):
    client, original, service, calls = ready
    imported = client.post("/api/documents/text", json={
        "title": "粘贴讲义", "text": "栈遵循后进先出，插入和删除在栈顶进行。此材料没有原始 PDF。"})
    assert imported.status_code == 200, imported.text
    document_id = imported.json()["id"]
    block = next(item for item in service.store.all("blocks") if item["document_id"] == document_id)
    text_card = {**original, "id": "pasted-text-review-card", "document_id": document_id,
                 "version": 1, "evidence": [{"block_id": block["block_id"], "quote": "栈遵循后进先出"}]}
    service.store.put("cards", text_card)
    response = post(client, text_card)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needs_review"
    assert calls == []


def test_manual_uploaded_picture_requires_human_review(ready):
    client, card, service, calls = ready
    stream = io.BytesIO()
    Image.new("RGB", (64, 64), "#aaccbb").save(stream, format="PNG")
    uploaded = client.post("/api/card-media", files={"file": ("manual.png", stream.getvalue(), "image/png")})
    assert uploaded.status_code == 200, uploaded.text
    changed = save(client, card, images=[{"media_id": uploaded.json()["id"], "side": "answer"}])
    assert changed.status_code == 200, changed.text
    card = changed.json()
    response = post(client, card)
    assert response.status_code == 200, response.text
    assert response.json()["status"] != "auto_pass"
    assert calls == []
    assert service.store.get("cards", card["id"])["status"] == "draft"


@pytest.mark.parametrize("classification,leakage", [("diagram", "no"), ("text", "yes"), ("table", "yes")])
def test_current_auto_passed_recommended_picture_allows_whole_card_review(saved_image, monkeypatch, classification, leakage):
    client, card, media, _job, _image_calls, image_service, storage = saved_image
    bind_imported_pdf(client, card, storage)
    from ankisense import image_auto_review
    from test_image_auto_review import GOOD
    monkeypatch.setattr(image_auto_review, "run_enrichment", lambda *args: {"result": {
        **GOOD, "classification": classification, "answer_leakage": leakage}})
    image_result = check_image(client, card, media)
    assert image_result.status_code == 200 and image_result.json()["status"] == "auto_pass"
    service = client.app.state.card_auto_review
    service.settings = image_service.settings
    calls = []

    async def review(_card, _blocks):
        calls.append(True)
        return good_review(card)

    monkeypatch.setattr(client.app.state.ai, "review_card", review)
    result = post(client, card)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "auto_pass" and len(calls) == 1
    approved = service.store.get("cards", card["id"])
    assert approved["status"] == "approved"
    from ankisense.approval import valid_machine_approval
    assert valid_machine_approval(service.store, approved)
    receipt = service.store.get("image_auto_reviews", image_result.json()["review_id"])
    receipt["policy_version"] = "saved-picture-auto-review-v4-direct"
    service.store.put("image_auto_reviews", receipt)
    assert not valid_machine_approval(service.store, approved)


def test_model_failure_needs_review_and_can_retry(ready, monkeypatch):
    client, card, _service, calls = ready

    async def fail(_card, _blocks):
        calls.append(True)
        raise AIError("模型调用超时，请稍后重试。")

    monkeypatch.setattr(client.app.state.ai, "review_card", fail)
    first = post(client, card)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "needs_review"
    assert first.json()["retryable"] is True and len(calls) == 1

    async def recover(_card, _blocks):
        calls.append(True)
        return good_review(card)

    monkeypatch.setattr(client.app.state.ai, "review_card", recover)
    second = post(client, card)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "auto_pass" and len(calls) == 2


def test_change_during_model_call_cannot_persist_pass(ready, monkeypatch):
    client, card, service, calls = ready
    entered, release = Event(), Event()

    async def slow(_card, _blocks):
        calls.append(True)
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)
        return good_review(card)

    monkeypatch.setattr(client.app.state.ai, "review_card", slow)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(post, client, card)
        assert entered.wait(5)
        changed = service.store.get("cards", card["id"])
        changed["question"] = "检查期间修改了题目。"
        changed["version"] += 1
        service.store.put("cards", changed)
        release.set()
        response = future.result(timeout=10)
    assert response.status_code == 409, response.text
    assert service.store.all("card_auto_reviews") == []


def test_pending_source_can_pass_and_later_confirmation_preserves_receipt(ready):
    client, card, service, calls = ready
    page = service.store.get("source_pages", f"{card['document_id']}-p2")
    page["status"] = "pending"
    page.pop("reviewed_at", None)
    service.store.put("source_pages", page)
    result = post(client, card).json()
    assert result["status"] == "auto_pass"
    assert card["id"] in client.get("/api/export").text
    saved = save_page_revision(service.store, card["document_id"], 2, 1,
                               {"text": page["text"], "status": "reviewed", "reviewed_at": "now"}, "proofread")
    assert saved["revision"] == 1
    assert client.get(route(card)).json()["review_id"] == result["review_id"]
    assert post(client, card).json()["cached"] is True and len(calls) == 1


def test_failed_source_invalidates_receipt_and_recheck_clears_machine_approval(ready):
    client, card, service, calls = ready
    assert post(client, card).json()["status"] == "auto_pass"
    page = service.store.get("source_pages", f"{card['document_id']}-p2")
    page["status"] = "failed"
    service.store.put("source_pages", page)
    assert client.get(route(card)).json()["status"] == "needs_review"
    result = post(client, card).json()
    assert result["status"] == "needs_review" and len(calls) == 1
    current = service.store.get("cards", card["id"])
    assert current["status"] == "draft" and not current.get("approval")


def test_whole_card_check_runs_missing_image_check_and_preserves_its_receipt(saved_image, monkeypatch):
    client, card, media, _job, image_calls, image_service, storage = saved_image
    bind_imported_pdf(client, card, storage)
    service = client.app.state.card_auto_review
    service.settings = image_service.settings
    page = service.store.get("source_pages", f"{card['document_id']}-p2")
    page["status"] = "pending"
    service.store.put("source_pages", page)
    async def review(_card, _blocks):
        return good_review(card)
    monkeypatch.setattr(client.app.state.ai, "review_card", review)
    initial = client.get(route(card)).json()
    assert initial["status"] == "unchecked" and initial["current"] is False
    assert image_calls == []  # Reading offers a check; it never starts one.
    result = post(client, card)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "auto_pass" and len(image_calls) == 1
    approved = service.store.get("cards", card["id"])
    assert approved["status"] == "approved" and approved["version"] == card["version"]
    image_result = check_image(client, card, media).json()
    assert image_result["status"] == "auto_pass" and image_result["cached"] is True
    assert len(image_calls) == 1


def test_queue_returns_progress_deduplicates_and_limits_active_model_calls(ready, monkeypatch):
    client, card, service, calls = ready
    cards = [card, {**card, "id": "queued-second"}, {**card, "id": "queued-third"}]
    service.store.put_many([("cards", entry) for entry in cards[1:]])
    second_started, release = Event(), Event()
    active = 0
    maximum = 0
    async def slow(_card, _blocks):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        calls.append(True)
        if len(calls) == 2:
            second_started.set()
        assert await asyncio.to_thread(release.wait, 10)
        active -= 1
        return good_review(card)
    monkeypatch.setattr(client.app.state.ai, "review_card", slow)
    try:
        for entry in cards:
            queued = client.portal.call(service.queue, entry["id"], entry["version"])
            assert queued["status"] in {"queued", "running"}
        assert second_started.wait(5)
        again = client.portal.call(service.queue, card["id"], card["version"])
        assert again["status"] == "running"
        assert len(calls) == 2 and len(service.running) == 3
        assert client.get(route(cards[2])).json()["status"] == "queued"
    finally:
        release.set()
    for entry in cards:
        assert post(client, entry).json()["status"] == "auto_pass"
    assert len(calls) == 3 and maximum == 2


def test_queue_does_not_retry_failed_model_but_explicit_post_does(ready, monkeypatch):
    client, card, service, calls = ready
    async def fail(_card, _blocks):
        calls.append(True)
        raise AIError("模型调用超时，请重试。")
    monkeypatch.setattr(client.app.state.ai, "review_card", fail)
    assert post(client, card).json()["retryable"] is True
    queued = client.portal.call(service.queue, card["id"], card["version"])
    assert queued["cached"] is True and len(calls) == 1
    assert post(client, card).json()["retryable"] is True and len(calls) == 2


def test_recovery_marks_interrupted_without_model_request(ready):
    client, card, service, calls = ready
    service.store.put("cards", {**card, "review_state": "running", "review_task_fingerprint": "old-task"})
    client.portal.call(service.recover)
    recovered = service.store.get("cards", card["id"])
    assert recovered["review_state"] == "interrupted" and "review_task_fingerprint" not in recovered
    result = client.get(route(card)).json()
    assert result["status"] == "needs_review" and result["retryable"] is True and calls == []
