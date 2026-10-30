"""Saved-picture review must fail closed and never approve a whole card."""
import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from ankisense import image_auto_review as auto
from ankisense.llm import Settings
from ankisense.sources import save_page_revision
from test_figure_recommendations import adopt, completed, context, save


GOOD = {"classification": "diagram", "description": "图示有栈顶和出入栈箭头。", "relation": "supported",
        "completeness": "complete", "legibility": "clear", "answer_leakage": "no",
        "reasons": ["裁图中标注和箭头完整。"]}


@pytest.fixture
def saved(context, monkeypatch):
    client, card, adapter, storage = context
    job = completed(client, card)
    media = adopt(client, card, job, bbox=[.1, .1, .6, .6]).json()
    attachment = {"media_id": media["id"], "side": "answer", "caption": "栈示意图"}
    updated = save(client, card, images=[attachment]).json()
    assert updated["version"] == 2 and updated["status"] == "draft"
    service = client.app.state.image_auto_review
    service.settings = Settings(mode="cloud", base_url="https://model.invalid/v1", model="fixture-vision", api_key="test-key")
    calls = []

    def fake_run(settings, crop, page, prompt):
        calls.append((crop, page, prompt))
        return {"result": copy.deepcopy(GOOD), "diagnostics": {"usage": {"total_tokens": 12}}}

    monkeypatch.setattr(auto, "capability", lambda _: {"available": True, "message": "available"})
    monkeypatch.setattr(auto, "run_enrichment", fake_run)
    return client, updated, media, job, calls, service, storage


def route(card, media):
    return f"/api/cards/{card['id']}/images/{media['id']}/auto-check"


def post(client, card, media):
    return client.post(route(card, media), json={"card_version": card["version"]})


def test_saved_picture_passes_only_explicitly_and_card_remains_draft(saved):
    client, card, media, _job, calls, service, _storage = saved
    before = client.get(route(card, media)).json()
    assert before["status"] == "unchecked" and before["current"] is False and calls == []
    response = post(client, card, media)
    assert response.status_code == 200, response.text
    checked = response.json()
    assert checked["status"] == "auto_pass" and checked["current"] is True and not checked["cached"]
    assert checked["visual"]["classification"] == "diagram"
    assert len(calls) == 1 and calls[0][0] != calls[0][1]
    assert card["question"] in calls[0][2]
    assert post(client, card, media).json()["cached"] is True and len(calls) == 1
    assert client.get(route(card, media)).json()["status"] == "auto_pass"
    record = service.store.get("image_auto_reviews", checked["review_id"])
    assert record["image_hash"] and record["pdf_hash"] and record["source_revision"] == 1
    assert record["card_version"] == 2 and record["policy_version"] == auto.POLICY_VERSION
    assert service.store.get("cards", card["id"])["status"] == "draft"


def test_pending_page_and_metadata_confirmation_reuse_same_image_result(saved):
    client, card, media, _job, calls, service, _storage = saved
    source_page = service.store.get("source_pages", f"{card['document_id']}-p2")
    source_page["status"] = "pending"
    service.store.put("source_pages", source_page)
    result = post(client, card, media).json()
    assert result["status"] == "auto_pass"
    source_page["status"] = "reviewed"
    service.store.put("source_pages", source_page)
    approved = {**card, "status": "approved", "approval": {"method": "machine"}, "updated_at": "now"}
    service.store.put("cards", approved)
    again = post(client, card, media).json()
    assert again["review_id"] == result["review_id"] and again["cached"] is True
    assert len(calls) == 1


@pytest.mark.parametrize("mutation", ["missing_candidate", "wrong_revision", "custom_crop", "cancelled_job"])
def test_local_risk_gates_do_not_call_model(saved, mutation):
    client, card, media, job, calls, service, _storage = saved
    if mutation in {"missing_candidate", "cancelled_job"}:
        changed = service.store.get("figure_jobs", job["id"])
        if mutation == "missing_candidate":
            changed["candidates"] = []
        else:
            changed["status"] = "failed"
        service.store.put("figure_jobs", changed)
    else:
        changed = service.store.get("media", media["id"])
        if mutation == "wrong_revision":
            changed["source"]["revision"] += 1
        else:
            changed["source"]["bbox"] = [.2, .2, .5, .5]
        service.store.put("media", changed)
    result = post(client, card, media)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "needs_review" and result.json()["reasons"]
    assert calls == []


@pytest.mark.parametrize("mutation", ["card", "source", "pdf", "image", "model"])
def test_prior_pass_is_invalidated_by_any_binding_change(saved, mutation):
    client, card, media, _job, calls, service, storage = saved
    assert post(client, card, media).json()["status"] == "auto_pass"
    if mutation == "card":
        changed = save(client, card, question="栈为什么后进先出？").json()
        assert changed["version"] == 3
    elif mutation == "source":
        save_page_revision(service.store, card["document_id"], 2, 1,
                           {"text": "栈遵循后进先出，已重新校对。", "status": "reviewed", "reviewed_at": "now"}, "proofread")
    elif mutation == "pdf":
        path = storage / "uploads" / f"{card['document_id']}.pdf"
        path.write_bytes(path.read_bytes() + b"\n% revised")
    elif mutation == "image":
        path = storage / "media" / f"{media['id']}.png"
        path.write_bytes(path.read_bytes() + b"changed")
    else:
        service.settings = Settings(mode="cloud", base_url="https://model.invalid/v1", model="new-vision", api_key="test-key")
    fetched = client.get(route(card, media))
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] != "auto_pass" and fetched.json()["current"] is False
    assert fetched.json()["stale"] is True
    assert len(calls) == 1


@pytest.mark.parametrize("change,side,expected", [
    ({"relation": "unrelated"}, "answer", "attention"),
    ({"completeness": "incomplete"}, "answer", "attention"),
    ({"legibility": "unclear"}, "answer", "attention"),
    ({"classification": "table"}, "answer", "auto_pass"),
    ({"classification": "text", "answer_leakage": "yes"}, "answer", "auto_pass"),
    ({"classification": "text", "answer_leakage": "uncertain"}, "answer", "auto_pass"),
    ({"classification": "text", "answer_leakage": "yes"}, "question", "attention"),
    ({"classification": "text"}, "question", "auto_pass"),
    ({"classification": "photo"}, "answer", "auto_pass"),
    ({"classification": "formula"}, "answer", "auto_pass"),
    ({"classification": "chart"}, "answer", "auto_pass"),
    ({"classification": "decoration"}, "answer", "attention"),
    ({"classification": "unknown"}, "answer", "needs_review"),
    ({"relation": "uncertain"}, "answer", "needs_review"),
    ({"answer_leakage": "yes"}, "question", "attention"),
    ({"answer_leakage": "uncertain"}, "question", "needs_review"),
])
def test_model_outcomes_fail_closed(saved, monkeypatch, change, side, expected):
    client, card, media, _job, calls, service, _storage = saved
    if side == "question":
        card = save(client, card, images=[{"media_id": media["id"], "side": "question"}]).json()
    def fake(settings, crop, page, prompt):
        calls.append(1)
        return {"result": {**GOOD, **change}}
    monkeypatch.setattr(auto, "run_enrichment", fake)
    result = post(client, card, media)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == expected
    assert len(calls) == 1
    assert service.store.get("cards", card["id"])["status"] == "draft"


def test_model_error_needs_review_and_can_retry(saved, monkeypatch):
    client, card, media, _job, calls, _service, _storage = saved
    def fail(*args):
        raise auto.VisionError("图像检查超时，请稍后重试。", {"error_code": "timeout"})
    monkeypatch.setattr(auto, "run_enrichment", fail)
    first = post(client, card, media)
    assert first.status_code == 200 and first.json()["status"] == "needs_review"
    assert "超时" in first.json()["reasons"][0]
    assert client.get(route(card, media)).json()["status"] == "needs_review"
    monkeypatch.setattr(auto, "run_enrichment", lambda *args: {"result": copy.deepcopy(GOOD)})
    assert post(client, card, media).json()["status"] == "auto_pass"


def test_second_cited_page_changes_invalidate_prior_pass(saved):
    client, card, media, _job, calls, service, _storage = saved
    extra = {"block_id": f"{card['document_id']}-p3-b1-r1", "quote": "栈遵循后进先出"}
    card = save(client, card, evidence=[*card["evidence"], extra]).json()
    assert post(client, card, media).json()["status"] == "auto_pass"
    third = service.store.get("source_pages", f"{card['document_id']}-p3")
    third["status"] = "extracted"
    service.store.put("source_pages", third)
    fetched = client.get(route(card, media)).json()
    assert fetched["status"] == "needs_review" and fetched["current"] is False
    assert len(calls) == 1


def test_change_during_model_check_does_not_persist_pass(saved, monkeypatch):
    client, card, media, _job, _calls, service, _storage = saved
    entered, release = Event(), Event()

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return {"result": copy.deepcopy(GOOD)}

    monkeypatch.setattr(auto, "run_enrichment", slow)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(post, client, card, media)
        assert entered.wait(5)
        changed = service.store.get("cards", card["id"])
        changed["answer"] = "已更新的答案。"
        service.store.put("cards", changed)
        release.set()
        assert future.result().status_code == 409
    assert service.store.all("image_auto_reviews") == []


def test_unattached_or_missing_card_and_version_never_run_model(saved):
    client, card, media, _job, calls, _service, _storage = saved
    assert client.get(f"/api/cards/missing/images/{media['id']}/auto-check").status_code == 404
    assert client.get(f"/api/cards/{card['id']}/images/img-{'a' * 32}/auto-check").status_code == 404
    assert client.post(route(card, media), json={"card_version": 1}).status_code == 409
    assert calls == []


def test_policy_upgrade_invalidates_old_results_without_automatic_model_call(saved, monkeypatch):
    client, card, media, _job, calls, service, _storage = saved
    current_policy = auto.POLICY_VERSION
    monkeypatch.setattr(auto, "POLICY_VERSION", "saved-picture-auto-review-v4-direct")
    old = post(client, card, media).json()
    assert old["status"] == "auto_pass" and len(calls) == 1
    monkeypatch.setattr(auto, "POLICY_VERSION", current_policy)
    fetched = client.get(route(card, media)).json()
    assert fetched["current"] is False and fetched["stale"] is True
    assert fetched["status"] != "auto_pass" and len(calls) == 1
    fresh = post(client, card, media).json()
    assert fresh["status"] == "auto_pass" and fresh["review_id"] != old["review_id"]
    assert len(calls) == 2


def test_full_source_page_with_answer_text_uses_same_policy(saved, monkeypatch):
    import hashlib
    from ankisense.media import normalize_image
    from ankisense.ocr import render_page_png
    client, card, media, _job, calls, service, storage = saved
    pdf = (storage / "uploads" / f"{card['document_id']}.pdf").read_bytes()
    page = media["source"]["page"]
    image = normalize_image(render_page_png(pdf, page))[0]
    (storage / "media" / f"{media['id']}.png").write_bytes(image)
    media["source"] = {"origin": "automatic_source_page", "page": page,
                       "revision": 1, "pdf_hash": hashlib.sha256(pdf).hexdigest()}
    service.store.put("media", media)
    monkeypatch.setattr(auto, "run_enrichment", lambda *args: {"result": {**GOOD,
        "classification": "text", "answer_leakage": "yes"}})
    result = post(client, card, media)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "auto_pass", result.text
    assert service.store.get("cards", card["id"])["status"] == "draft"
