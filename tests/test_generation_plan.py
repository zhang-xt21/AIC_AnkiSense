"""Planning is local and source preserving, including partial-range disclosure."""
import copy

import pytest
from fastapi import HTTPException

from ankisense.generation_plan import build_plan, public_plan
from ankisense.schemas import SourceBlock


class MemoryStore:
    def __init__(self):
        self.data = {"documents": [{"id": "doc", "pdf_sha256": "original-pdf"}],
                     "source_pages": [], "blocks": []}

    def all(self, collection):
        return copy.deepcopy(self.data.get(collection, []))

    def get(self, collection, identifier):
        return next((item for item in self.all(collection)
                     if item.get("id", item.get("block_id")) == identifier), None)

    def page(self, number, text="栈在栈顶插入和删除，遵循后进先出。", status="pending", revision=1):
        identifier = f"doc-p{number}-b1-r{revision}"
        self.data["source_pages"].append({"id": f"doc-p{number}", "document_id": "doc",
                                         "page": number, "text": text, "status": status,
                                         "revision": revision, "block_ids": [identifier]})
        self.data["blocks"].append({"block_id": identifier, "document_id": "doc", "page": number, "text": text})
        return identifier


def assert_limits(plan):
    assert 1 <= plan["target_count"] <= 30
    assert sum(batch["count"] for batch in plan["batches"]) == plan["target_count"]
    for batch in plan["batches"]:
        assert 1 <= batch["count"] <= 6
        assert 1 <= len(batch["blocks"]) <= 6
        assert batch["chars"] == sum(len(block["text"]) for block in batch["blocks"]) <= 16000
        assert len({block["block_id"] for block in batch["blocks"]}) == len(batch["blocks"])
        for block in batch["blocks"]:
            SourceBlock.model_validate(block)


def test_default_uses_all_valid_current_sources_without_confirmation_or_writes():
    store = MemoryStore()
    store.page(1)
    store.page(2, "队列从队尾插入，从队头删除。", status="reviewed")
    store.page(3, "遗留失败识别", status="failed")
    store.page(4, " \n ", status="empty")
    store.data["blocks"].append({"block_id": "doc-p1-b1-r0", "document_id": "doc", "page": 1, "text": "过期"})
    before = copy.deepcopy(store.data)
    plan = build_plan(store, "doc")
    assert plan["source_pages"] == [1, 2]
    assert plan["stats"]["source_block_count"] == 2
    assert plan["skipped_pages"] == [{"page": 3, "reason": "文字提取失败"}, {"page": 4, "reason": "没有可用文字"}]
    assert plan["target_count"] == plan["recommended_count"] == 1
    assert plan["partial_scope"] is False
    assert "知识点总数" in plan["suggestion_reason"]
    assert store.data == before
    assert_limits(plan)


def test_recommendation_covers_all_batches_and_remains_local():
    store = MemoryStore()
    for number in range(1, 31):
        store.page(number, f"本页知识点{number}。" * 50)
    plan = build_plan(store, "doc")
    assert plan["recommended_count"] >= len(plan["batches"])
    assert plan["selected_pages"] == list(range(1, 31))
    assert plan["stats"]["coverage_ratio"] == 1
    assert_limits(plan)


def test_small_explicit_target_uniformly_samples_and_reports_missing_sources():
    store = MemoryStore()
    for number in range(1, 31):
        store.page(number)
    plan = build_plan(store, "doc", count=2)
    assert plan["batches"][0]["pages"] == list(range(1, 7))
    assert plan["batches"][1]["pages"] == list(range(25, 31))
    assert plan["omitted_pages"] == list(range(7, 25))
    assert len(plan["omitted_blocks"]) == 18
    assert plan["partial_scope"] and plan["warnings"]
    assert plan["stats"]["coverage_ratio"] == .4
    assert_limits(plan)


def test_long_single_block_windows_preserve_id_and_exact_characters():
    store = MemoryStore()
    text = "第一节：栈与队列\n\n" + "这是一段原文，必须保持引用位置。\n\n" * 1800
    identifier = store.page(1, text)
    plan = build_plan(store, "doc", count=6)
    actual = [block for batch in plan["batches"] for block in batch["blocks"]]
    assert len(actual) >= 2
    assert all(block["block_id"] == identifier for block in actual)
    assert "".join(block["text"] for block in actual) == text
    assert all(block["text"] in text for block in actual)
    assert plan["partial_scope"] is False
    assert_limits(plan)


def test_partial_single_page_does_not_claim_full_page_coverage():
    store = MemoryStore()
    store.page(1, "长原文内容。" * 8000)
    plan = build_plan(store, "doc", count=1)
    assert plan["selected_pages"] == [1]
    assert plan["omitted_pages"] == []
    assert plan["partial_pages"] == [1]
    assert plan["partial_scope"]
    assert plan["stats"]["coverage_ratio"] < 1
    assert any("未包含" in warning for warning in plan["warnings"])
    assert_limits(plan)


@pytest.mark.parametrize("target", [1, 2, 6, 7, 12, 17, 30])
def test_user_count_allocates_bounded_batches_even_with_short_material(target):
    store = MemoryStore()
    store.page(1)
    plan = build_plan(store, "doc", count=target)
    assert plan["target_count"] == target
    assert plan["recommended_count"] == 1
    assert plan["stats"]["coverage_ratio"] == 1
    if target > 6:
        assert any("相同原文" in warning for warning in plan["warnings"])
    assert_limits(plan)


def test_many_requested_cards_split_long_contexts_instead_of_truncating():
    store = MemoryStore()
    text = "标题\n" + "栈和队列的不同特性。\n" * 300
    store.page(1, text)
    plan = build_plan(store, "doc", count=30)
    assert len(plan["batches"]) == 5
    assert "".join(block["text"] for batch in plan["batches"] for block in batch["blocks"]) == text
    assert plan["stats"]["coverage_ratio"] == 1
    assert_limits(plan)


def test_selected_scope_keeps_page_order_not_checkbox_order():
    store = MemoryStore()
    ids = [store.page(number) for number in [10, 2, 1]]
    plan = build_plan(store, "doc", block_ids=ids[:2])
    assert plan["scope"] == "selected"
    assert plan["source_pages"] == [2, 10]
    assert [block["page"] for block in plan["batches"][0]["blocks"]] == [2, 10]
    assert plan["source_fingerprint"] == build_plan(store, "doc", block_ids=list(reversed(ids[:2])))["source_fingerprint"]


@pytest.mark.parametrize("selection,status", [([], 422), (["unknown"], 404), (["foreign"], 422),
                                             (["doc-p1-b1-r0"], 409), (["doc-p2-b1-r1"], 409),
                                             (["doc-p1-b1-r1"] * 2, 422)])
def test_explicit_invalid_selection_is_not_silently_dropped(selection, status):
    store = MemoryStore()
    store.page(1)
    store.page(2, "失败文字", status="failed")
    store.data["blocks"].extend([
        {"block_id": "foreign", "document_id": "other", "page": 1, "text": "其他讲义"},
        {"block_id": "doc-p1-b1-r0", "document_id": "doc", "page": 1, "text": "旧文字"},
    ])
    with pytest.raises(HTTPException) as error:
        build_plan(store, "doc", block_ids=selection)
    assert error.value.status_code == status


def test_fingerprint_changes_for_real_sources_not_confirmation_or_requested_count():
    store = MemoryStore()
    store.page(1)
    before = build_plan(store, "doc")["source_fingerprint"]
    page = store.data["source_pages"][0]
    page.update(status="reviewed", reviewed_at="later", updated_at="later")
    assert build_plan(store, "doc", count=17)["source_fingerprint"] == before
    store.data["blocks"][0]["text"] += "补充文字。"
    assert build_plan(store, "doc")["source_fingerprint"] != before


def test_fingerprint_changes_when_skipped_page_becomes_available():
    store = MemoryStore()
    store.page(1)
    store.page(2, "已有文字但识别失败", status="failed")
    before = build_plan(store, "doc")["source_fingerprint"]
    store.data["source_pages"][1]["status"] = "pending"
    assert build_plan(store, "doc")["source_fingerprint"] != before


def test_plan_rejects_oversized_scope_empty_scope_and_unknown_document():
    store = MemoryStore()
    with pytest.raises(HTTPException) as error:
        build_plan(store, "unknown")
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        build_plan(store, "doc")
    assert error.value.status_code == 409
    store.page(1, "字" * 100001)
    with pytest.raises(HTTPException) as error:
        build_plan(store, "doc")
    assert error.value.status_code == 422


@pytest.mark.parametrize("count", [0, 31, -1, True, 2.5, "3"])
def test_invalid_counts_rejected(count):
    store = MemoryStore()
    store.page(1)
    with pytest.raises(HTTPException) as error:
        build_plan(store, "doc", count=count)
    assert error.value.status_code == 422


def test_legacy_text_sources_and_short_headings_are_kept_verbatim():
    store = MemoryStore()
    text = "  栈\n后进先出。  "
    store.data["blocks"].append({"block_id": "p1", "document_id": "doc", "page": 1, "text": text})
    plan = build_plan(store, "doc")
    assert plan["batches"][0]["blocks"][0]["text"] == text
    result = public_plan(plan)
    assert result["batches"][0]["block_ids"] == ["p1"]
    assert "blocks" not in result["batches"][0]
    assert result["min_count"] == 1 and result["max_count"] == 30
    assert "blocks" in plan["batches"][0]


def test_repeated_headers_are_retained_in_context():
    store = MemoryStore()
    for number in range(1, 5):
        store.page(number, "大学计算机学院\n" + f"第{number}节知识点：栈顶插入删除。\n" + "大学计算机学院")
    plan = build_plan(store, "doc")
    assert all(block["text"].count("大学计算机学院") == 2
               for batch in plan["batches"] for block in batch["blocks"])
    assert plan["stats"]["coverage_ratio"] == 1
