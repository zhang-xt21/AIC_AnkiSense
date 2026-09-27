"""Source-scoped local suggestions; parser calls use an offline test double."""

import importlib
import io
import json
import sys
import time
import types
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from ankisense.llm import Settings
from ankisense.schemas import CardUpdate, Diagnosis
from ankisense.sources import page_records, save_page_revision


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module-app"))
    monkeypatch.setenv("AI_MODE", "demo")
    adapter = types.ModuleType("ankisense.docling_adapter")
    adapter.PARSER_VERSION = "offline-regions-v1"
    adapter.calls = []

    def extract(path, pages, cache):
        adapter.calls.append(list(pages))
        return {"parser_version": adapter.PARSER_VERSION, "pages": [{"page": page, "width": 300, "height": 240,
                "regions": [{"kind": "picture", "bbox": [.1, .1, .6, .6], "caption": "栈示意图", "nearby_text": "栈遵循后进先出"},
                            {"kind": "table", "bbox": [.65, .1, .95, .7], "caption": "队列对比", "nearby_text": "队列示例"}]} for page in pages]}

    adapter.extract_regions = extract
    monkeypatch.setitem(sys.modules, "ankisense.docling_adapter", adapter)
    module = importlib.import_module("ankisense.app")
    app = module.create_app(data_dir=tmp_path / "test-app", settings=Settings(mode="demo"))
    store = app.state.store
    document_id = "pdf-" + "a" * 24
    writer = PdfWriter()
    for _ in range(3):
        page = writer.add_blank_page(width=300, height=240)
        stream = DecodedStreamObject()
        stream.set_data(b"q 1 0 0 rg 30 96 150 120 re f Q")  # Vector rectangle, no embedded image.
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    storage = store.path.parent
    (storage / "uploads").mkdir()
    (storage / "uploads" / f"{document_id}.pdf").write_bytes(output.getvalue())
    document = {"id": document_id, "title": "栈讲义", "filename": "lecture.pdf", "is_demo": False,
                "file_url": f"/api/documents/{document_id}/file", "page_count": 3}
    records = [("documents", document)]
    for page in range(1, 4):
        records.extend(page_records(document_id, {"page": page, "text": "栈遵循后进先出，插入和删除在栈顶进行。", "status": "reviewed", "reviewed_at": "2026-09-26"}))
    card = {"id": "figure-test-card", "document_id": document_id, "question": "栈遵循什么顺序？", "answer": "后进先出。",
            "concept": "栈", "kind": "basic", "rubric": [{"point_id": "stack_lifo", "text": "说明后进先出"}],
            "evidence": [{"block_id": f"{document_id}-p2-b1-r1", "quote": "栈遵循后进先出"}],
            "version": 1, "status": "approved", "is_demo": False}
    store.put_many(records + [("cards", card)])
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        yield client, card, adapter, storage


def start(client, card):
    return client.post(f"/api/cards/{card['id']}/image-recommendations", json={"card_version": card["version"]})


def wait_for(client, identifier):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/image-recommendations/{identifier}").json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(.015)
    pytest.fail("Local fixture recommendation did not finish")


def completed(client, card):
    response = start(client, card)
    assert response.status_code == 200, response.text
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "completed", job
    return job


def adopt(client, card, job, candidate=None, **changes):
    return client.post(f"/api/cards/{card['id']}/image-recommendations/adopt", json={
        "job_id": job["id"], "candidate_id": (candidate or job["candidates"][0])["id"], "card_version": card["version"], **changes})


def save(client, card, **changes):
    body = {key: card[key] for key in CardUpdate.model_fields if key in card}
    return client.put(f"/api/cards/{card['id']}", json={**body, **changes})


def test_only_explicitly_cited_pages_and_related_regions_are_recommended(context):
    client, card, adapter, storage = context
    job = completed(client, card)
    assert adapter.calls == [[2]]
    assert job["card_id"] == card["id"] and job["card_version"] == 1
    assert {item["page"] for item in job["candidates"]} == {2}
    assert [item["kind"] for item in job["candidates"]] == ["picture", "page"]
    assert "同页候选" in job["candidates"][0]["reason"]
    assert "未识别为独立配图" in job["candidates"][1]["reason"]
    for candidate in job["candidates"]:
        assert candidate["source_revision"] == 1
        assert client.get(candidate["preview_url"]).status_code == 200
        assert client.get(candidate["page_url"]).status_code == 200
    assert client.get(f"/api/image-recommendations/{job['id']}/pages/1/image").status_code == 404
    assert client.app.state.store.all("media") == []
    assert client.app.state.store.get("cards", card["id"]) == card


def test_jobs_reuse_completed_cache_and_rebuild_missing_page_preview(context):
    client, card, adapter, storage = context
    job = completed(client, card)
    assert completed(client, card)["id"] == job["id"]
    assert adapter.calls == [[2]]
    (storage / "figure-previews" / job["id"] / "page-2.png").unlink()
    rebuilt = completed(client, card)
    assert rebuilt["id"] != job["id"]
    assert adapter.calls == [[2]], "Region cache should avoid re-running the parser"
    # Changing card text invalidates recommendations but keeps the same page regions.
    edited = save(client, card, question="请解释栈的后进先出规则").json()
    newer = completed(client, edited)
    assert newer["card_version"] == 2 and newer["id"] != rebuilt["id"]
    assert adapter.calls == [[2]]
    adapter.PARSER_VERSION = "offline-regions-v2"
    assert completed(client, edited)["id"] != newer["id"]
    assert adapter.calls == [[2], [2]]


def test_parser_failure_and_missing_page_have_clear_fallback_and_allow_retry(context):
    client, card, adapter, storage = context
    original = adapter.extract_regions
    adapter.extract_regions = lambda *args: (_ for _ in ()).throw(RuntimeError("private path /secret/models"))
    failed = completed(client, card)
    assert failed["parser_failed"] is True
    assert all(item["kind"] == "page" for item in failed["candidates"])
    assert "原图" in failed["message"] and "secret" not in json.dumps(failed)
    assert adopt(client, card, failed).status_code == 200
    assert client.app.state.store.all("figure_pages") == []
    adapter.extract_regions = lambda *args: {"pages": []}
    missing = completed(client, card)
    assert missing["id"] != failed["id"] and missing["parser_failed"] is True
    assert client.app.state.store.all("figure_pages") == []
    adapter.extract_regions = original
    recovered = completed(client, card)
    assert recovered["id"] != missing["id"] and not recovered["parser_failed"]
    assert any(item["kind"] == "picture" for item in recovered["candidates"])


def test_full_page_and_manual_crop_preserve_vector_graphics_without_overwriting(context):
    client, card, _, storage = context
    job = completed(client, card)
    page = next(item for item in job["candidates"] if item["kind"] == "page")
    original = client.get(page["page_url"]).content
    media = adopt(client, card, job, page, bbox=[.1, .1, .6, .6])
    assert media.status_code == 200, media.text
    first = media.json()
    assert first["source"]["bbox"] == [.1, .1, .6, .6]
    assert first["source"]["card_version"] == 1
    assert first["source"]["origin"] == "figure_recommendation"
    with Image.open(io.BytesIO(client.get(first["url"]).content)) as image:
        assert image.getpixel((image.width // 2, image.height // 2))[:3] == (255, 0, 0)
    second = adopt(client, card, job, page).json()
    assert second["id"] != first["id"]
    assert client.get(first["url"]).status_code == 200
    assert client.get(page["page_url"]).content == original
    assert client.app.state.store.get("cards", card["id"]) == card


@pytest.mark.parametrize("bbox", [[-.1, 0, 1, 1], [0, 0, 1.1, 1], [.8, .2, .2, .8], [0, 0, .001, .001], [0, 0, .01, .01], [0, 0, 1]])
def test_crop_bounds_and_tiny_crops_are_rejected(context, bbox):
    client, card, _, _ = context
    job = completed(client, card)
    assert adopt(client, card, job, bbox=bbox).status_code == 422
    assert client.app.state.store.all("media") == []


def test_nan_crop_is_rejected_by_schema_without_writing_media(context):
    from pydantic import ValidationError
    from ankisense.schemas import ImageRecommendationAdoptRequest
    with pytest.raises(ValidationError):
        ImageRecommendationAdoptRequest(card_version=1, job_id="figjob-" + "a" * 32,
                                         candidate_id="fig-" + "b" * 32, bbox=[0, 0, float("nan"), 1])


@pytest.mark.parametrize("change", ["card", "source", "pdf"])
def test_stale_card_source_or_pdf_cannot_be_adopted(context, change):
    client, card, _, storage = context
    job = completed(client, card)
    store = client.app.state.store
    if change == "card":
        save(client, card, question="另一版本的问题")
    elif change == "source":
        save_page_revision(store, card["document_id"], 2, 1,
                           {"text": "栈遵循后进先出，原文已经修改。", "status": "reviewed", "reviewed_at": "now"}, "proofread")
    else:
        path = storage / "uploads" / f"{card['document_id']}.pdf"
        path.write_bytes(path.read_bytes() + b"\n% changed PDF content")
    response = adopt(client, card, job)
    assert response.status_code == 409, response.text
    assert store.all("media") == []


def test_adopt_then_edit_in_another_tab_cannot_save_stale_new_media(context):
    client, card, _, _ = context
    job = completed(client, card)
    media = adopt(client, card, job).json()
    edited = save(client, card, question="另一窗口修改后的问题").json()
    response = save(client, edited, images=[{"media_id": media["id"], "side": "answer"}])
    assert response.status_code == 409, response.text
    assert not client.app.state.store.get("cards", card["id"]).get("images")


def test_new_suggestion_requires_draft_then_explicit_review_and_exports_to_apkg(context, monkeypatch):
    client, card, _, _ = context
    media = adopt(client, card, completed(client, card)).json()
    first = save(client, card, status="approved", images=[{"media_id": media["id"], "side": "answer", "caption": "栈原图"}])
    assert first.status_code == 200, first.text
    draft = first.json()
    assert draft["status"] == "draft"
    assert client.get("/api/export/apkg").status_code == 422
    assert client.post("/api/attempts", json={"card_id": card["id"], "answer": "后进先出", "request_id": "unapproved-picture"}).status_code == 409
    approved = save(client, draft, status="approved").json()
    assert approved["status"] == "approved" and approved["id"] == card["id"]

    async def diagnose(card_model, blocks, answer):
        return Diagnosis(verdict="correct", missing_point_ids=[], contradicted_point_ids=[], answer_spans=[answer],
                         source_block_ids=[blocks[0].block_id], reason="离线测试回答与引用一致。")

    monkeypatch.setattr(client.app.state.ai, "diagnose_answer", diagnose)
    attempt = client.post("/api/attempts", json={"card_id": card["id"], "answer": "后进先出", "request_id": "approved-picture"}).json()
    assert attempt["status"] == "completed"
    assert attempt["card_snapshot"]["images"][0]["media_id"] == media["id"]
    exported = client.get("/api/export/apkg")
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read("media"))
        assert media["filename"] in manifest.values()
        filename = next(key for key, value in manifest.items() if value == media["filename"])
        assert archive.read(filename) == client.get(media["url"]).content


def test_same_card_concurrent_start_is_one_job_and_parser_is_serial(context):
    client, card, adapter, _ = context
    original = adapter.extract_regions
    entered, release = Event(), Event()

    def blocked(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    adapter.extract_regions = blocked
    first = start(client, card).json()
    try:
        assert entered.wait(10)
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(pool.map(lambda _: start(client, card).json(), range(3)))
        assert all(job["id"] == first["id"] for job in responses)
    finally:
        release.set()
    assert wait_for(client, first["id"])["status"] == "completed"
    assert adapter.calls == [[2]]


def test_cancelled_job_never_publishes_candidates_and_retry_succeeds(context):
    client, card, adapter, _ = context
    original = adapter.extract_regions
    entered, release = Event(), Event()

    def blocked(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    adapter.extract_regions = blocked
    job = start(client, card).json()
    try:
        assert entered.wait(10)
        cancelled = client.post(f"/api/image-recommendations/{job['id']}/cancel").json()
        assert cancelled["status"] == "failed" and cancelled["candidates"] == []
    finally:
        release.set()
    adapter.extract_regions = original
    retried = completed(client, card)
    assert retried["id"] != job["id"]
    assert client.get(f"/api/image-recommendations/{job['id']}").json()["candidates"] == []


def test_restart_marks_interrupted_jobs_failed(context):
    client, card, _, _ = context
    job = completed(client, card)
    store = client.app.state.store
    saved = store.get("figure_jobs", job["id"])
    saved.update(status="running", candidates=[])
    store.put("figure_jobs", saved)
    client.app.state.recommendations.recover()
    recovered = client.get(f"/api/image-recommendations/{job['id']}").json()
    assert recovered["status"] == "failed" and "重启" in recovered["message"]


def test_missing_pdf_unreviewed_source_and_other_card_rejected_without_job(context):
    client, card, _, storage = context
    assert start(client, {**card, "version": 9}).status_code == 409
    client.post("/api/demo")
    demo = client.app.state.store.get("cards", "demo-stack")
    assert start(client, demo).status_code == 422
    store = client.app.state.store
    page = store.get("source_pages", card["document_id"] + "-p2")
    store.put("source_pages", {**page, "status": "pending"})
    assert start(client, card).status_code == 409
    store.put("source_pages", page)
    (storage / "uploads" / f"{card['document_id']}.pdf").unlink()
    assert start(client, card).status_code == 422
    assert store.all("figure_jobs") == []


def test_new_suggestion_preserves_explicit_rejected_status(context):
    client, card, _, _ = context
    media = adopt(client, card, completed(client, card)).json()
    response = save(client, card, status="rejected", images=[{"media_id": media["id"], "side": "answer"}])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    assert client.get("/api/export/apkg").status_code == 422


@pytest.mark.parametrize("field,value", [("question", "改成新的问题？"), ("concept", "修改后的概念"), ("evidence", None)])
def test_adopt_then_change_text_before_first_save_rejects_new_suggestion(context, field, value):
    client, card, _, _ = context
    media = adopt(client, card, completed(client, card)).json()
    if field == "evidence":
        value = [{"block_id": card["evidence"][0]["block_id"], "quote": "后进先出"}]
    response = save(client, card, **{field: value}, images=[{"media_id": media["id"], "side": "answer"}])
    assert response.status_code == 409, response.text
    assert "移除本次新配图" in response.json()["detail"]
    assert client.app.state.store.get("cards", card["id"]) == card


def test_already_saved_image_is_retained_when_user_manually_edits_card(context):
    client, card, _, _ = context
    media = adopt(client, card, completed(client, card)).json()
    first = save(client, card, images=[{"media_id": media["id"], "side": "answer"}]).json()
    edited = save(client, first, question="人工选择保留原图的新问题？", status="draft")
    assert edited.status_code == 200, edited.text
    assert edited.json()["images"][0]["media_id"] == media["id"]


def test_ranking_version_invalidates_jobs_without_reparsing_regions(context, monkeypatch):
    from ankisense import figure_recommendations
    client, card, adapter, _ = context
    old = completed(client, card)
    monkeypatch.setattr(figure_recommendations, "RECOMMENDATION_VERSION", "new-filter-fixture")
    new = completed(client, card)
    assert new["id"] != old["id"]
    assert adapter.calls == [[2]]


@pytest.mark.parametrize("include_real_diagram", [False, True])
def test_unrelated_edge_decorations_do_not_become_recommended_diagrams(context, include_real_diagram):
    client, card, adapter, _ = context
    ornaments = [
        {"kind": "picture", "bbox": [.80, .02, .97, .16], "caption": "", "nearby_text": "栈遵循后进先出"},
        {"kind": "picture", "bbox": [.01, .76, .22, .99], "caption": "", "nearby_text": ""},
        {"kind": "picture", "bbox": [0, .93, 1, 1], "caption": "", "nearby_text": "后进先出"},
    ]
    if include_real_diagram:
        ornaments.append({"kind": "picture", "bbox": [.2, .3, .8, .7], "caption": "栈示意图", "nearby_text": "后进先出"})
    adapter.extract_regions = lambda path, pages, cache: {"pages": [{"page": page, "regions": ornaments} for page in pages]}
    job = completed(client, card)
    diagrams = [candidate for candidate in job["candidates"] if candidate["kind"] != "page"]
    assert len(diagrams) == int(include_real_diagram)
    if diagrams:
        assert diagrams[0]["bbox"] == [.185, .285, .815, .715]
    assert [candidate["page"] for candidate in job["candidates"] if candidate["kind"] == "page"] == [2]


@pytest.mark.parametrize("bbox,expected", [
    ([.015, .18, .345, .515], [0, .165, .36, .53]),
    ([.655, .18, .985, .515], [.64, .165, 1, .53]),
    ([.0183524, .1735655, .3510391, .5114395], [.0033524, .1585655, .3660391, .5264395]),
])
def test_uncaptioned_body_diagram_touching_side_edge_is_preserved(context, bbox, expected):
    client, card, adapter, _ = context
    region = {"kind": "picture", "bbox": bbox, "caption": "", "nearby_text": "栈遵循后进先出"}
    adapter.extract_regions = lambda path, pages, cache: {"pages": [{"page": page, "regions": [region]} for page in pages]}
    job = completed(client, card)
    diagrams = [candidate for candidate in job["candidates"] if candidate["kind"] != "page"]
    assert len(diagrams) == 1
    assert diagrams[0]["bbox"] == expected
    assert "同页候选" in diagrams[0]["reason"]


@pytest.mark.parametrize("bbox", [
    [.0130654, .7416223, .254814, .9885477],
    [.014, .748, .251, .989],
    [.014, .752, .249, .989],
    [.7395137, .003213, .9865192, .0757076],
])
def test_small_corner_decorations_are_stable_across_quarter_page_boundaries(context, bbox):
    client, card, adapter, _ = context
    region = {"kind": "picture", "bbox": bbox, "caption": "", "nearby_text": "栈遵循后进先出"}
    adapter.extract_regions = lambda path, pages, cache: {"pages": [{"page": page, "regions": [region]} for page in pages]}
    assert all(candidate["kind"] == "page" for candidate in completed(client, card)["candidates"])


@pytest.mark.parametrize("bbox", [[.01, .74, .26, .99], [.0114512, .7399282, .2582614, .987888]])
def test_matching_caption_without_position_does_not_rescue_small_corner_ornament(context, bbox):
    client, card, adapter, _ = context
    region = {"kind": "picture", "bbox": bbox, "caption": "后进先出示意图", "nearby_text": ""}
    adapter.extract_regions = lambda path, pages, cache: {"pages": [{"page": page, "regions": [region]} for page in pages]}
    assert all(candidate["kind"] == "page" for candidate in completed(client, card)["candidates"])


@pytest.mark.parametrize("bbox", [[.015, .18, .345, .515], [.3, .01, .7, .14]])
def test_relevant_caption_preserves_body_or_wider_header_diagram(context, bbox):
    client, card, adapter, _ = context
    region = {"kind": "picture", "bbox": bbox, "caption": "后进先出示意图", "nearby_text": ""}
    adapter.extract_regions = lambda path, pages, cache: {"pages": [{"page": page, "regions": [region]} for page in pages]}
    assert any(candidate["kind"] == "picture" for candidate in completed(client, card)["candidates"])


@pytest.mark.parametrize("kind,text,keep", [
    ("table", ">\n>\n>\n>", True),
    ("picture", "例：4 + 2 * 3 – 12 / (7 – 5)\nθ1 θ2\n+\n*", True),
    ("picture", "A B C + - 123", True),
    ("table", "网络协议通过地址定位设备，路由转发数据包。", False),
])
def test_symbol_only_regions_are_unconfirmed_candidates_but_unrelated_prose_is_filtered(context, kind, text, keep):
    client, card, adapter, _ = context
    region = {"kind": kind, "bbox": [.2, .25, .8, .7], "caption": "", "nearby_text": text}
    adapter.extract_regions = lambda path, pages, cache: {"pages": [{"page": page, "regions": [region]} for page in pages]}
    diagrams = [candidate for candidate in completed(client, card)["candidates"] if candidate["kind"] != "page"]
    assert len(diagrams) == int(keep)
    if keep:
        assert diagrams[0]["kind"] == kind
        assert "尚未确认内容关联" in diagrams[0]["reason"]


@pytest.mark.parametrize("bbox,expected", [
    ([.005, .2, .995, .99], [0, .185, 1, 1]),
    ([.1, .005, .7, .5], [.085, 0, .715, .515]),
])
def test_automatic_padding_is_clamped_and_preserved_on_adopt_without_changing_manual_crop(context, bbox, expected):
    client, card, adapter, _ = context
    region = {"kind": "picture", "bbox": bbox, "caption": "后进先出示意图", "nearby_text": ""}
    adapter.extract_regions = lambda path, pages, cache: {"pages": [{"page": page, "regions": [region]} for page in pages]}
    job = completed(client, card)
    candidate = next(item for item in job["candidates"] if item["kind"] == "picture")
    assert candidate["bbox"] == expected
    assert next(item for item in job["candidates"] if item["kind"] == "page")["bbox"] == [0, 0, 1, 1]
    automatic = adopt(client, card, job, candidate)
    assert automatic.status_code == 200, automatic.text
    assert automatic.json()["source"]["bbox"] == expected
    assert client.get(automatic.json()["url"]).content == client.get(candidate["preview_url"]).content
    manual_bbox = [.2, .3, .4, .5]
    manual = adopt(client, card, job, candidate, bbox=manual_bbox)
    assert manual.status_code == 200, manual.text
    assert manual.json()["source"]["bbox"] == manual_bbox


def test_final_publish_rechecks_card_version_inside_write_transaction(context, monkeypatch):
    client, card, _, _ = context
    service = client.app.state.recommendations
    original = service._transition
    changed = []

    def edit_immediately_before_publish(job_id, expected, **updates):
        if updates.get("status") == "completed" and not changed:
            changed.append(True)
            client.app.state.store.put("cards", {**card, "question": "发布候选前刚刚修改的问题", "version": 2})
        return original(job_id, expected, **updates)

    monkeypatch.setattr(service, "_transition", edit_immediately_before_publish)
    response = start(client, card)
    assert response.status_code == 200, response.text
    job = wait_for(client, response.json()["id"])
    assert changed
    assert job["status"] == "failed"
    assert job["candidates"] == []
    assert "更新" in job["message"]
