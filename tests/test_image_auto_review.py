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
    original_extract = adapter.extract_regions

    def extract_with_layout(*args):
        result = original_extract(*args)
        for page in result["pages"]:
            page["regions"] = [item for item in page["regions"] if item["kind"] == "picture"]
            page["text_regions"] = []  # A real successful layout pass, with no crop quality warning.
        return result

    adapter.extract_regions = extract_with_layout
    job = completed(client, card)
    media = adopt(client, card, job).json()
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


@pytest.mark.parametrize("mutation", ["quality_flag", "multiple_pictures", "parser_failed", "whole_page", "custom_crop"])
def test_local_risk_gates_do_not_call_model(saved, mutation):
    client, card, media, job, calls, service, _storage = saved
    source = media["source"]
    if mutation in {"quality_flag", "multiple_pictures", "parser_failed"}:
        changed = service.store.get("figure_jobs", job["id"])
        if mutation == "quality_flag":
            changed["candidates"][0]["quality_flags"] = ["text_at_crop_edge"]
        elif mutation == "multiple_pictures":
            changed["candidates"].append({**changed["candidates"][0], "id": "fig-other"})
        else:
            changed["parser_failed"] = True
        service.store.put("figure_jobs", changed)
    elif mutation == "whole_page":
        page_candidate = next(item for item in job["candidates"] if item["kind"] == "page")
        changed = service.store.get("media", media["id"])
        changed["source"]["candidate_id"] = page_candidate["id"]
        changed["source"]["bbox"] = page_candidate["bbox"]
        service.store.put("media", changed)
    else:
        changed = service.store.get("media", media["id"])
        changed["source"]["bbox"] = [.2, .2, .5, .5]
        service.store.put("media", changed)
    result = post(client, card, media)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "needs_review" and result.json()["reasons"]
    assert calls == []


@pytest.mark.parametrize("mutation", ["hidden_second_picture", "hidden_table", "truncated_layout", "legacy_layout"])
def test_unranked_page_layout_must_be_complete_and_single_picture(saved, mutation):
    client, card, media, _job, calls, service, _storage = saved
    page = media["source"]["page"]
    layout = next(item for item in service.store.all("figure_pages") if item["page"] == page)
    if mutation == "hidden_second_picture":
        assert len([item for item in layout["regions"] if item["kind"] == "picture"]) == 1
        layout["regions"].append({**layout["regions"][0], "bbox": [.5, .5, .7, .7]})
    elif mutation == "hidden_table":
        layout["regions"].append({**layout["regions"][0], "kind": "table", "bbox": [.5, .5, .7, .7]})
    elif mutation == "legacy_layout":
        layout.pop("regions_truncated")
    else:
        layout["regions_truncated"] = True
    service.store.put("figure_pages", layout)
    result = post(client, card, media)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "needs_review"
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
    ({"classification": "table"}, "answer", "needs_review"),
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


@pytest.mark.parametrize("caption", ["", None, "missing", "栈遵循后进先出"])
def test_page_corner_furniture_does_not_count_as_a_second_diagram(saved, caption):
    client, card, media, _job, calls, service, _storage = saved
    layout = next(item for item in service.store.all("figure_pages") if item["page"] == 2)
    ornaments = [{"kind": "picture", "bbox": [.739, .003, .986, .077]},
                 {"kind": "picture", "bbox": [.013, .753, .252, .984]}]
    for ornament in ornaments:
        if caption != "missing":
            ornament["caption"] = caption
        # Real slides may assign the diagram's nearby text/caption to a corner
        # flower or university logo. Corner geometry still identifies furniture.
        ornament["nearby_text"] = "栈遵循后进先出，插入和删除在栈顶进行。"
    layout["regions"].extend(ornaments)
    service.store.put("figure_pages", layout)
    response = post(client, card, media)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "auto_pass", response.json()
    assert len(calls) == 1
    assert len(service.store.get("figure_pages", layout["id"])["regions"]) == 3, "raw layout must remain intact"
    cached = client.get(route(card, media)).json()
    assert cached["cached"] is True and cached["status"] == "auto_pass"


@pytest.mark.parametrize("region,expected", [
    ({"kind": "picture", "bbox": [.35, .93, .8, .98]}, "auto_pass"),
    ({"kind": "picture", "bbox": [.35, .02, .8, .10], "caption": "栈遵循后进先出"}, "needs_review"),
    ({"kind": "picture", "bbox": [.65, .2, .95, .6]}, "needs_review"),
    ({"kind": "table", "bbox": [.74, .003, .986, .077]}, "needs_review"),
    ({"kind": "formula", "bbox": [.74, .003, .986, .077]}, "needs_review"),
    ({"kind": "picture", "bbox": [0, 0, 0, 0]}, "needs_review"),
    ({"kind": "picture"}, "needs_review"),
])
def test_only_unambiguous_picture_ornaments_are_filtered(saved, region, expected):
    client, card, media, _job, calls, service, _storage = saved
    layout = next(item for item in service.store.all("figure_pages") if item["page"] == 2)
    layout["regions"].append(region)
    service.store.put("figure_pages", layout)
    response = post(client, card, media)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected
    assert len(calls) == (1 if expected == "auto_pass" else 0)


@pytest.mark.parametrize("change", ["wrong_original", "legacy_padded", "unrelated_raw_region"])
def test_candidate_must_match_the_remaining_raw_content_region(saved, change):
    client, card, media, job, calls, service, _storage = saved
    current = service.store.get("figure_jobs", job["id"])
    candidate = next(item for item in current["candidates"] if item["id"] == media["source"]["candidate_id"])
    if change == "wrong_original":
        candidate["original_bbox"] = [.65, .2, .95, .6]
        service.store.put("figure_jobs", current)
    elif change == "legacy_padded":
        candidate.pop("original_bbox")
        service.store.put("figure_jobs", current)
    else:
        layout = next(item for item in service.store.all("figure_pages") if item["page"] == 2)
        layout["regions"][0]["bbox"] = [.65, .2, .95, .6]
        service.store.put("figure_pages", layout)
    response = post(client, card, media)
    assert response.status_code == 200, response.text
    expected = "auto_pass" if change == "legacy_padded" else "needs_review"
    assert response.json()["status"] == expected
    assert len(calls) == (1 if expected == "auto_pass" else 0)


def test_full_raw_layout_still_participates_in_the_review_fingerprint(saved):
    client, card, media, _job, calls, service, _storage = saved
    assert post(client, card, media).json()["status"] == "auto_pass"
    layout = next(item for item in service.store.all("figure_pages") if item["page"] == 2)
    layout["regions"].append({"kind": "picture", "bbox": [.74, .003, .986, .077]})
    service.store.put("figure_pages", layout)
    result = client.get(route(card, media)).json()
    assert result["current"] is False and result["stale"] is True
    assert len(calls) == 1, "GET must not silently refresh a review after layout changes"
