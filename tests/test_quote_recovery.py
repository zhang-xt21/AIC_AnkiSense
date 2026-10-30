import asyncio
import json
from types import SimpleNamespace

import pytest

from ankisense.db import Store
from ankisense.schemas import SourceBlock
from ankisense.quote_recovery import recover_points, valid_supplement, region_for
from ankisense.sources import require_current_source
from test_automatic_import import client, upload, wait_import


def setup(tmp_path):
    store = Store(tmp_path / "quote.db")
    block = SourceBlock(block_id="doc-p1-b1-r1", document_id="doc", page=1, text="面的表现）：第一，实行措施甲。第二，实行措施乙。")
    store.put("blocks", block.model_dump())
    store.put("source_pages", {"id": "doc-p1", "document_id": "doc", "page": 1, "revision": 1,
                              "status": "pending", "block_ids": [block.block_id], "text": block.text,
                              "raw_text": block.text, "ocr_lines": [{"text": block.text, "bbox": [.1,.5,.9,.6]}]})
    full = "②社会主义任务（3个方面的表现）：第一，实行措施甲。第二，实行措施乙。"
    raw = {"knowledge_id": "k1", "concept": "任务", "statement": "实行措施甲和措施乙。", "conditions": [],
           "evidence": [{"block_id": block.block_id, "quote": full}]}
    rejected = [{"knowledge_id": "k1", "reason": "引用必须是对应讲义片段中的连续原文。", "draft_preview": json.dumps(raw, ensure_ascii=False)}]
    return store, block, full, rejected


@pytest.mark.parametrize("local_ok", [True, False])
def test_local_then_visual_is_bounded_cached_and_source_bound(tmp_path, monkeypatch, local_ok):
    import ankisense.quote_recovery as module
    from PIL import Image
    from io import BytesIO
    store, block, full, rejected = setup(tmp_path)
    counts = {"ocr": 0, "visual": 0}
    def ocr(*args):
        counts["ocr"] += 1
        return {"text": full if local_ok else "识别失败"}
    async def visual(image):
        assert image.startswith(b"\x89PNG")
        counts["visual"] += 1
        return SimpleNamespace(text=full, uncertainties=[])
    data = BytesIO(); Image.new("RGB", (300,300), "white").save(data, format="PNG")
    monkeypatch.setattr(module, "recognize_region", ocr)
    monkeypatch.setattr(module, "render_page_png", lambda *args: data.getvalue())
    ai = SimpleNamespace(transcribe_source_page=visual)
    async def run():
        result = await recover_points(store, b"pdf", [block], rejected, ai, lambda: None, "j1")
        points, failures, extra = result
        assert len(points) == len(extra) == 1 and not failures
        assert extra[0].text == full
        assert store.get("blocks", block.block_id)["text"] == block.text
        page = store.get("source_pages", "doc-p1")
        require_current_source(store, extra)
        assert valid_supplement(store, extra[0], page)
        await recover_points(store, b"pdf", [block], rejected, ai, lambda: None, "j1")
        assert counts == {"ocr": 1, "visual": 0 if local_ok else 1}
        page["revision"] = 2
        assert not valid_supplement(store, extra[0], page)
    asyncio.run(run())


def test_no_anchor_does_not_call_ocr_or_api(tmp_path):
    store, block, full, rejected = setup(tmp_path)
    page = store.get("source_pages", "doc-p1"); page["ocr_lines"] = []; store.put("source_pages", page)
    points, failures, extra = asyncio.run(recover_points(store,b"pdf",[block],rejected,object(),lambda:None,"j1"))
    assert not points and not extra and failures
    assert not store.all("quote_recoveries")


def test_unrelated_quote_cannot_get_a_crop():
    assert region_for("完全不同的另一段没有任何相同内容", "原文与引用不相同", [{"text": "原文", "bbox": [.1,.2,.9,.3]}]) is None


def test_automatic_import_uses_supplement_for_review_practice_and_export(client, monkeypatch):
    import ankisense.quote_recovery as module
    from ankisense.draft_validation import KnowledgePointSelection
    from ankisense.sources import save_page_revision
    ai = client.app.state.ai
    original = ai.extract_knowledge_visual
    texts = {}
    async def extract(blocks, focus_ids, images, numbers):
        batch = await original(blocks, focus_ids, images, numbers)
        raw = batch.points[0].model_dump()
        parent = blocks[0]
        full = "Missing heading: " + parent.text
        texts[parent.page] = full
        raw["evidence"][0]["quote"] = full
        page = client.app.state.store.get("source_pages", f"{parent.document_id}-p{parent.page}")
        page.update(raw_text=page["text"], ocr_lines=[{"text": parent.text, "bbox": [.1,.5,.9,.6]}])
        client.app.state.store.put("source_pages", page)
        return KnowledgePointSelection(points=list(batch.points[1:]), rejected_points=[{
            "knowledge_id": raw["knowledge_id"], "reason": "引用必须是对应讲义片段中的连续原文。",
            "draft_preview": json.dumps(raw)}])
    ai.extract_knowledge_visual_partial = extract
    monkeypatch.setattr(module, "recognize_region", lambda pdf, page, bbox: {"text": texts[page]})
    result = wait_import(client, upload(client))
    assert result["status"] == "completed", result
    store = client.app.state.store
    card = next(card for card in store.all("cards") if card["evidence"][0]["block_id"].startswith("quote-fix-"))
    assert card["status"] == "approved"
    assert client.app.state.attempts._current_sources(card)
    assert client.app.state.plugin.sync._validated(card, store)
    assert card["id"] in client.get("/api/export").text
    source = store.get("source_pages", f"{card['document_id']}-p1")
    save_page_revision(store,card["document_id"],1,source["revision"],{"text":"Changed source", "status":"pending"},"test")
    assert store.get("cards",card["id"])["source_outdated"]


def test_uncertain_visual_read_is_not_saved_or_repeated(tmp_path, monkeypatch):
    import ankisense.quote_recovery as module
    from PIL import Image
    from io import BytesIO
    store, block, full, rejected = setup(tmp_path)
    data=BytesIO(); Image.new("RGB",(200,200),"white").save(data,format="PNG")
    monkeypatch.setattr(module,"render_page_png",lambda *args:data.getvalue())
    monkeypatch.setattr(module,"recognize_region",lambda *args:{"text":"missing"})
    calls=[]
    async def visual(image):
        calls.append(1); return SimpleNamespace(text=full, uncertainties=["模糊"])
    async def run():
        for _ in range(2):
            fixed, remaining, extra=await recover_points(store,b"pdf",[block],rejected,SimpleNamespace(transcribe_source_page=visual),lambda:None,"j1")
            assert not fixed and remaining and not extra
    asyncio.run(run())
    assert len(calls)==1
