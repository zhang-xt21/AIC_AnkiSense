"""Cited-page preparation never auto-adds a whole page or invokes a model."""
import asyncio
import copy
from threading import Event

import pytest

from ankisense.automatic_illustrations import AutomaticIllustrationService
from test_figure_recommendations import context


@pytest.fixture
def pipeline(context):
    client, card, renderer, storage = context
    store = client.app.state.store
    card.update(status="draft", image_intent={"need": "helpful", "reason": "示意图辅助理解。",
                                              "block_ids": [card["evidence"][0]["block_id"]]})
    store.put("cards", card)
    reviews = []
    async def review(saved):
        reviews.append(copy.deepcopy(saved))
        return store.get("cards", saved["id"])
    service = AutomaticIllustrationService(store, client.app.state.recommendations,
                                           client.app.state.image_auto_review, review)
    yield client, card, renderer, service, reviews, store
    client.portal.call(service.shutdown)


def finish(client, service):
    async def wait():
        await asyncio.gather(*list(service.running.values()))
    client.portal.call(wait)


def start(pipeline, **options):
    client, card, _, service, _, store = pipeline
    return client.portal.call(lambda: service.queue(store.get("cards", card["id"]), **options))


@pytest.mark.parametrize("need", ["helpful", "required"])
def test_original_pages_are_prepared_for_explicit_selection(pipeline, need):
    client, card, renderer, service, reviews, store = pipeline
    card["image_intent"]["need"] = need
    store.put("cards", card)
    start(pipeline)
    finish(client, service)
    saved = store.get("cards", card["id"])
    assert saved["auto_illustration"]["state"] == "needs_review"
    assert "裁剪" in saved["auto_illustration"]["message"]
    assert not saved.get("images") and saved["version"] == 1
    assert renderer.calls == [2]
    assert len(reviews) == (1 if need == "helpful" else 0)
    assert store.all("media") == [] and store.all("image_auto_reviews") == []


@pytest.mark.parametrize("mode", ["off", "none", "existing"])
def test_skip_preserves_existing_content_without_preparing_pages(pipeline, mode):
    client, card, renderer, service, reviews, store = pipeline
    if mode == "none":
        card["image_intent"]["need"] = "none"
    elif mode == "existing":
        card["images"] = [{"media_id": "existing-image", "side": "answer"}]
    store.put("cards", card)
    start(pipeline, enabled=mode != "off")
    finish(client, service)
    saved = store.get("cards", card["id"])
    assert saved["auto_illustration"]["state"] == "skipped"
    assert saved.get("images") == card.get("images")
    assert renderer.calls == [] and len(reviews) == 1


def test_explicit_retry_reuses_page_cache_without_changing_card(pipeline):
    client, card, renderer, service, reviews, store = pipeline
    start(pipeline); finish(client, service)
    first = store.get("cards", card["id"])["auto_illustration"]["job_id"]
    start(pipeline, retry=True); finish(client, service)
    current = store.get("cards", card["id"])
    assert current["auto_illustration"]["job_id"] == first
    assert current["version"] == 1 and not current.get("images")
    assert renderer.calls == [2]


def test_restart_marks_interrupted_without_starting_work(pipeline):
    client, card, renderer, service, reviews, store = pipeline
    card["auto_illustration"] = {"state": "checking", "task_id": "old", "card_version": 1}
    store.put("cards", card)
    client.portal.call(service.recover)
    assert store.get("cards", card["id"])["auto_illustration"]["state"] == "interrupted"
    assert not renderer.calls and not reviews


def test_shutdown_does_not_start_new_page_work(pipeline):
    client, card, renderer, service, reviews, store = pipeline
    service.closing = True
    start(pipeline)
    assert store.get("cards", card["id"])["auto_illustration"]["state"] == "interrupted"
    assert not renderer.calls and not reviews


def test_changed_card_during_page_render_preserves_new_content(pipeline):
    client, card, renderer, service, reviews, store = pipeline
    entered, release = Event(), Event()
    original = renderer.render
    def blocked(*args):
        entered.set(); assert release.wait(10)
        return original(*args)
    renderer.render = blocked
    start(pipeline)
    try:
        assert entered.wait(10)
        store.put("cards", {**store.get("cards", card["id"]), "question": "新题目", "version": 2})
    finally:
        release.set()
    finish(client, service)
    saved = store.get("cards", card["id"])
    assert saved["version"] == 2 and saved["question"] == "新题目" and not saved.get("images")
