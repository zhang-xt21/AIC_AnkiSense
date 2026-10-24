"""Automatic lecture pictures use local fixtures and one mocked vision request."""
import asyncio
import copy
from threading import Event

import pytest

from ankisense import image_auto_review as auto
from ankisense.automatic_illustrations import AutomaticIllustrationService
from ankisense.figure_vision import VisionError
from ankisense.llm import Settings
from ankisense.sources import save_page_revision
from test_figure_recommendations import context
from test_image_auto_review import GOOD


@pytest.fixture
def pipeline(context, monkeypatch):
    client, card, adapter, storage = context
    original = adapter.extract_regions

    def clean_layout(*args):
        result = original(*args)
        for page in result["pages"]:
            page["regions"] = [region for region in page["regions"] if region["kind"] == "picture"]
            page["text_regions"] = []
        return result

    adapter.extract_regions = clean_layout
    store = client.app.state.store
    card.update(status="draft", image_intent={"need": "helpful", "reason": "栈顺序可由示意图辅助理解。",
                                              "block_ids": [card["evidence"][0]["block_id"]]})
    store.put("cards", card)
    image_review = client.app.state.image_auto_review
    image_review.settings = Settings(mode="cloud", base_url="https://model.invalid/v1", model="fixture-vision", api_key="test-key")
    calls, reviews = [], []

    def fake_run(settings, crop, page, prompt):
        calls.append(prompt)
        return {"result": copy.deepcopy(GOOD)}

    async def review(saved):
        reviews.append(copy.deepcopy(saved))
        latest = store.get("cards", saved["id"])
        latest.update(status="approved", review_state="auto_pass", approval={"method": "machine"})
        store.put("cards", latest)
        return latest

    monkeypatch.setattr(auto, "capability", lambda _: {"available": True})
    monkeypatch.setattr(auto, "run_enrichment", fake_run)
    service = AutomaticIllustrationService(store, client.app.state.recommendations, image_review, review)
    yield client, card, adapter, service, calls, reviews, store
    client.portal.call(service.shutdown)


def finish(client, service):
    async def wait():
        await asyncio.gather(*list(service.running.values()))
    client.portal.call(wait)


def start(pipeline, **options):
    client, card, _, service, _, _, store = pipeline
    async def queue():
        return await service.queue(store.get("cards", card["id"]), **options)
    return client.portal.call(queue)


def test_helpful_picture_is_checked_before_attach_and_reuses_receipt(pipeline):
    client, card, adapter, service, calls, reviews, store = pipeline
    start(pipeline)
    finish(client, service)
    updated = store.get("cards", card["id"])
    assert updated["auto_illustration"]["state"] == "attached", updated
    assert updated["version"] == 2 and updated["status"] == "approved"
    assert updated["images"][0]["side"] == "answer"
    assert [item["version"] for item in reviews] == [1, 2]
    assert not reviews[0].get("images")
    assert len(calls) == 1 and adapter.calls == [[2]]
    media_id = updated["images"][0]["media_id"]
    checked = client.get(f"/api/cards/{card['id']}/images/{media_id}/auto-check").json()
    assert checked["status"] == "auto_pass" and checked["cached"] is True
    assert len(calls) == 1
    receipt = store.get("image_auto_reviews", checked["review_id"])
    assert receipt["card_version"] == updated["version"]


def test_required_image_is_on_question_side_and_text_review_waits(pipeline):
    client, card, _, service, calls, reviews, store = pipeline
    card["image_intent"]["need"] = "required"
    store.put("cards", card)
    start(pipeline)
    finish(client, service)
    updated = store.get("cards", card["id"])
    assert updated["auto_illustration"]["state"] == "attached"
    assert updated["images"][0]["side"] == "question"
    assert [item["version"] for item in reviews] == [2]
    assert len(calls) == 1


@pytest.mark.parametrize("need,expected", [("helpful", "approved"), ("required", "draft")])
def test_failed_visual_never_attaches_or_retries(pipeline, monkeypatch, need, expected):
    client, card, _, service, calls, reviews, store = pipeline
    card["image_intent"]["need"] = need
    store.put("cards", card)

    def fail(*args):
        calls.append("timeout")
        raise VisionError("视觉检查超时。")

    monkeypatch.setattr(auto, "run_enrichment", fail)
    start(pipeline)
    finish(client, service)
    current = store.get("cards", card["id"])
    assert current["auto_illustration"]["state"] == "failed"
    assert current["version"] == 1 and current["status"] == expected
    assert not current.get("images") and not store.all("image_auto_reviews")
    assert len(calls) == 1
    start(pipeline)
    finish(client, service)
    assert len(calls) == 1, "ordinary repeated queue must not retry a failed paid request"


@pytest.mark.parametrize("change", ["leak", "unrelated", "incomplete"])
def test_uncertain_or_unsafe_required_figure_is_not_attached(pipeline, monkeypatch, change):
    client, card, _, service, calls, reviews, store = pipeline
    card["image_intent"]["need"] = "required"
    store.put("cards", card)
    result = copy.deepcopy(GOOD)
    result.update({"answer_leakage": "yes"} if change == "leak" else
                  {"relation": "unrelated"} if change == "unrelated" else {"completeness": "incomplete"})
    monkeypatch.setattr(auto, "run_enrichment", lambda *args: {"result": result})
    start(pipeline)
    finish(client, service)
    current = store.get("cards", card["id"])
    assert current["auto_illustration"]["state"] == "needs_review"
    assert not current.get("images") and current["status"] == "draft"
    assert reviews == []


@pytest.mark.parametrize("layout", ["empty", "multiple", "table", "warning"])
def test_ambiguous_or_missing_regions_never_call_vision(pipeline, layout):
    client, card, adapter, service, calls, reviews, store = pipeline
    original = adapter.extract_regions

    def changed(*args):
        result = original(*args)
        for page in result["pages"]:
            if layout == "empty":
                page["regions"] = []
            elif layout == "multiple":
                page["regions"].append({**page["regions"][0], "bbox": [.65, .2, .9, .8]})
            elif layout == "table":
                # Hidden tables are absent from ranked candidates, but the
                # complete original layout still prevents an automatic pass.
                page["regions"].append({**page["regions"][0], "kind": "table", "bbox": [.65, .2, .9, .8]})
            else:
                page["warning"] = "layout incomplete"
        return result

    adapter.extract_regions = changed
    start(pipeline)
    finish(client, service)
    current = store.get("cards", card["id"])
    assert current["auto_illustration"]["state"] in {"not_found", "needs_review"}
    assert not current.get("images") and current["status"] == "approved"
    assert calls == []


@pytest.mark.parametrize("mode", ["none", "disabled", "existing"])
def test_skip_preserves_existing_content_without_parser_or_vision(pipeline, mode):
    client, card, adapter, service, calls, reviews, store = pipeline
    if mode == "none":
        card["image_intent"]["need"] = "none"
    if mode == "existing":
        card["images"] = [{"media_id": "img-" + "a" * 32, "side": "answer"}]
    store.put("cards", card)
    start(pipeline, enabled=mode != "disabled")
    finish(client, service)
    current = store.get("cards", card["id"])
    assert current["auto_illustration"]["state"] == "skipped"
    assert current.get("images") == card.get("images") and current["version"] == 1
    assert calls == adapter.calls == []
    assert len(reviews) == 1


@pytest.mark.parametrize("mutation", ["card", "source", "model"])
def test_changed_binding_discards_candidate_without_overwriting(pipeline, monkeypatch, mutation):
    client, card, _, service, calls, reviews, store = pipeline
    entered, release = Event(), Event()

    def wait_for_edit(*args):
        entered.set()
        assert release.wait(10)
        calls.append("checked")
        return {"result": copy.deepcopy(GOOD)}

    monkeypatch.setattr(auto, "run_enrichment", wait_for_edit)
    start(pipeline)
    assert entered.wait(10)
    if mutation == "card":
        current = store.get("cards", card["id"])
        current.update(version=2, question="我修改的题目")
        store.put("cards", current)
    elif mutation == "source":
        save_page_revision(store, card["document_id"], 2, 1,
                           {"text": "栈遵循后进先出，原文已补充。", "status": "reviewed", "reviewed_at": "now"}, "proofread")
    else:
        service.image_auto_review.settings = Settings(mode="cloud", base_url="https://new.invalid", model="other", api_key="test-key")
    release.set()
    finish(client, service)
    current = store.get("cards", card["id"])
    assert not current.get("images") and not store.all("image_auto_reviews")
    assert current["auto_illustration"]["state"] == "interrupted"
    if mutation == "card":
        assert current["question"] == "我修改的题目" and current["version"] == 2
    assert len(calls) == 1 and len(reviews) == 1


def test_same_page_reuses_local_layout_across_cards(pipeline):
    client, card, adapter, service, calls, reviews, store = pipeline
    start(pipeline)
    finish(client, service)
    second = {**card, "id": "second-figure-card", "question": "栈的插入和删除在哪端进行？"}
    store.put("cards", second)
    client.portal.call(service.queue, second)
    finish(client, service)
    assert store.get("cards", second["id"])["auto_illustration"]["state"] == "attached"
    assert adapter.calls == [[2]], "Docling should parse a shared cited page only once"
    assert len(calls) == 2, "each distinct card still needs its own relation check"


def test_restart_marks_interrupted_without_automatic_work(pipeline):
    client, card, adapter, service, calls, reviews, store = pipeline
    card["auto_illustration"] = {"state": "checking", "card_version": 1}
    store.put("cards", card)
    client.portal.call(service.recover)
    assert store.get("cards", card["id"])["auto_illustration"]["state"] == "interrupted"
    assert calls == adapter.calls == reviews == []


def test_shutdown_does_not_start_queued_parsing_or_vision(pipeline):
    client, card, adapter, service, calls, reviews, store = pipeline

    async def queue_then_close():
        await service.queue(card)
        await service.shutdown()

    client.portal.call(queue_then_close)
    assert store.get("cards", card["id"])["auto_illustration"]["state"] == "interrupted"
    assert calls == adapter.calls == []


def test_explicit_retry_uses_cached_layout_and_keeps_old_text_card(pipeline, monkeypatch):
    client, card, adapter, service, calls, reviews, store = pipeline
    monkeypatch.setattr(auto, "capability", lambda _: {"available": False, "message": "尚未配置视觉模型。"})
    start(pipeline)
    finish(client, service)
    assert store.get("cards", card["id"])["auto_illustration"]["state"] == "failed"
    monkeypatch.setattr(auto, "capability", lambda _: {"available": True})
    start(pipeline, retry=True)
    finish(client, service)
    assert store.get("cards", card["id"])["auto_illustration"]["state"] == "attached"
    assert len(calls) == 1 and adapter.calls == [[2]]


def test_passed_candidate_and_receipt_roll_back_together(pipeline, monkeypatch):
    client, card, _, service, calls, reviews, store = pipeline
    original_mutate = store.mutate

    def reject_receipt_transaction(build, check=None):
        def validate():
            records = build()
            if any(collection == "image_auto_reviews" for collection, _ in records):
                assert any(collection == "cards" for collection, _ in records)
                raise RuntimeError("simulated storage failure")
            return records
        return original_mutate(validate, check=check)

    monkeypatch.setattr(store, "mutate", reject_receipt_transaction)
    start(pipeline)
    finish(client, service)
    current = store.get("cards", card["id"])
    assert current["auto_illustration"]["state"] == "failed"
    assert not current.get("images") and current["version"] == 1
    assert not store.all("image_auto_reviews") and len(calls) == 1
    assert current["status"] == "approved", "the useful prior text card is still available"


def test_shutdown_during_current_visual_preserves_image_but_starts_no_text_request(pipeline, monkeypatch):
    client, card, _, service, calls, reviews, store = pipeline
    card["image_intent"]["need"] = "required"
    store.put("cards", card)
    entered, release = Event(), Event()

    def blocked(*args):
        entered.set()
        assert release.wait(10)
        calls.append("one")
        return {"result": copy.deepcopy(GOOD)}

    monkeypatch.setattr(auto, "run_enrichment", blocked)
    start(pipeline)
    assert entered.wait(10)
    service.closing = True
    release.set()
    client.portal.call(service.shutdown)
    current = store.get("cards", card["id"])
    assert current["auto_illustration"]["state"] == "attached"
    assert current["review_state"] == "interrupted" and current["status"] == "draft"
    assert len(calls) == 1 and reviews == []


@pytest.mark.parametrize("intended_has_picture", [True, False])
def test_only_image_intent_pages_supply_the_attachment(pipeline, intended_has_picture):
    client, card, adapter, service, calls, reviews, store = pipeline
    card["evidence"].append({"block_id": f"{card['document_id']}-p3-b1-r1", "quote": "栈遵循后进先出"})
    store.put("cards", card)
    original = adapter.extract_regions

    def layouts(*args):
        result = original(*args)
        if not intended_has_picture:
            next(page for page in result["pages"] if page["page"] == 2)["regions"] = []
        return result

    adapter.extract_regions = layouts
    start(pipeline)
    finish(client, service)
    current = store.get("cards", card["id"])
    if intended_has_picture:
        assert current["auto_illustration"]["state"] == "attached", current
        assert current["auto_illustration"]["page"] == 2 and len(calls) == 1
    else:
        assert current["auto_illustration"]["state"] == "not_found"
        assert not current.get("images") and calls == []


def test_missing_docling_message_remains_actionable(pipeline):
    client, card, adapter, service, calls, reviews, store = pipeline

    class MissingParser(RuntimeError):
        code = "unavailable"

    def missing(*args):
        raise MissingParser()

    adapter.extract_regions = missing
    start(pipeline)
    finish(client, service)
    state = store.get("cards", card["id"])["auto_illustration"]
    assert state["state"] == "needs_review" and "尚未安装" in state["message"]
    assert calls == []


def test_shutdown_during_candidate_capture_does_not_start_vision(pipeline, monkeypatch):
    client, card, _, service, calls, reviews, store = pipeline
    entered, release = Event(), Event()
    original_capture = service.image_auto_review._capture

    def capture(*args, **kwargs):
        result = original_capture(*args, **kwargs)
        if kwargs.get("card_override"):
            entered.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(service.image_auto_review, "_capture", capture)
    start(pipeline)
    assert entered.wait(10)
    service.closing = True
    release.set()
    client.portal.call(service.shutdown)
    current = store.get("cards", card["id"])
    assert current["auto_illustration"]["state"] == "interrupted"
    assert not current.get("images") and calls == []
