"""Regression gates from the outline pilot; doubles do not measure model quality."""
import asyncio

import pytest

from ankisense.auto_cards import automatic_plan
from ankisense.auto_card_models import KnowledgeBatch, KnowledgePoint
from ankisense.draft_validation import select_knowledge_points
from ankisense.job_progress import summarize_batches
from ankisense.learning_policy import balanced_points, non_learning_reason
from ankisense.reference_check import reference_answer_passes
from ankisense.schemas import CardDraft, Diagnosis, SourceBlock
from test_automatic_import import client, lecture_pdf, wait_import


def source(page=1):
    return SourceBlock(block_id=f"b{page}", document_id="doc", page=page,
                       text="定义甲。定义乙。定义丙。定义丁。定义戊。定义己。")


@pytest.mark.parametrize("pages,count", [(1,8),(2,8),(4,8),(1,600),(10,3),(4,None)])
def test_budget_is_fully_allocated_without_exceeding_one_model_call_capacity(pages,count):
    blocks=[source(p) for p in range(1,pages+1)]
    plan=automatic_plan(blocks,count)
    assert len({b["batch_id"] for b in plan["batches"]}) == plan["total_batches"]
    if count is not None:
        assert sum(b["count"] for b in plan["batches"]) == count
        assert all(1 <= b["count"] <= 6 for b in plan["batches"])
    if count and count >= pages:
        assert plan["selected_pages"] == list(range(1,pages+1))


def test_four_pages_receive_two_slots_each_instead_of_four_for_the_last_page():
    plan=automatic_plan([source(p) for p in range(1,5)],8)
    assert [(b["pages"],b["count"]) for b in plan["batches"]] == [([1],2),([2],2),([3],2),([4],2)]


def test_low_budget_explicitly_reports_omitted_sources():
    plan=automatic_plan([source(p) for p in range(1,5)],2)
    assert plan["partial_scope"] and plan["omitted_pages"] == [2,3]
    assert plan["source_coverage_ratio"] == .5


def test_repeated_source_passes_remain_distinct_pending_checkpoints():
    plan=automatic_plan([source()],8)
    first={**plan["batches"][0],"status":"completed"}
    summary,details=summarize_batches({"status":"interrupted","progress":{"total":2,"completed":1},
                                      "plan":plan,"batches":[first]})
    assert summary["successful"] == 1 and summary["pending"] == 1
    assert len(details) == 1 and details[0]["status"] == "pending"


def test_legacy_plan_without_hash_still_matches_a_saved_hashed_checkpoint():
    job={"status":"interrupted","progress":{"total":1,"completed":1},
         "plan":{"batches":[{"block_ids":["old"],"pages":[1]}]},
         "batches":[{"batch_id":"legacy-hash","block_ids":["old"],"pages":[1],"status":"completed"}]}
    summary,details=summarize_batches(job)
    assert summary["pending"] == 0 and details == []


@pytest.mark.parametrize("concept,text", [
    ("课程基本信息","课程编码、学分、学时是多少？"),
    ("学时分配","理论与网络课堂各多少学时？"),
    ("课程性质与先修要求","适用对象和先修要求？"),
    ("课程目标CO1","要求掌握哪些内容？"),
    ("实验项目类型","实验1到8的类型？"),
    ("考勤要求","本课程的考勤规则？"),
])
def test_observed_administrative_card_targets_are_excluded(concept,text):
    assert non_learning_reason(concept,text)


@pytest.mark.parametrize("concept,text", [
    ("关系模式设计","如何用SQL统计学生学分？"),
    ("随机变量","实验的可能结果如何表示？"),
    ("目录结构","文件目录如何支持按名称查找文件？"),
])
def test_domain_facts_are_retained_when_they_mention_metadata_words(concept,text):
    assert non_learning_reason(concept,text) is None


def test_point_filter_retains_the_valid_neighbor_and_records_excluded_content():
    b=source()
    def raw(identifier,concept):
        return {"knowledge_id":identifier,"concept":concept,"statement":"定义甲。","conditions":[],
                "evidence":[{"block_id":b.block_id,"quote":"定义甲。"}]}
    result=select_knowledge_points([raw("admin","课程基本信息"),raw("domain","定义")],[b],[b.block_id])
    assert [p.knowledge_id for p in result.points] == ["domain"]
    assert not result.rejected_points
    assert result.filtered_points[0]["knowledge_id"] == "admin"


def test_candidate_selection_prefers_an_unrepresented_page():
    blocks=[source(1),source(2)]
    def point(identifier,b):
        return KnowledgePoint(knowledge_id=identifier,concept=identifier,statement="定义甲。",
                              evidence=[{"block_id":b.block_id,"quote":"定义甲。"}])
    points=[point("p1a",blocks[0]),point("p1b",blocks[0]),point("p2",blocks[1])]
    assert [p.knowledge_id for p in balanced_points(points,blocks,2)] == ["p1a","p2"]


def test_a_correct_label_without_point_coverage_is_not_a_reference_answer_pass():
    b=source()
    card=CardDraft(question="定义甲是什么？",answer="定义甲。",concept="定义甲",
                   rubric=[{"point_id":"fact","text":"定义甲。"}],
                   evidence=[{"block_id":b.block_id,"quote":"定义甲。"}])
    result=Diagnosis(verdict="correct",addressed_point_ids=[],missing_point_ids=[],contradicted_point_ids=[],
                     answer_spans=[card.answer],source_block_ids=[b.block_id],reason="空的要点核对结果。")
    assert not reference_answer_passes(result,card,[b])
    result.addressed_point_ids=["fact"]
    assert reference_answer_passes(result,card,[b])
    assert not reference_answer_passes(None,card,[b])


def test_reference_mismatch_is_kept_for_review_and_never_exported(client):
    async def incomplete(card,blocks,answer):
        return Diagnosis(verdict="incomplete",addressed_point_ids=["fact"],missing_point_ids=["fact"],
                         contradicted_point_ids=[],answer_spans=[answer],source_block_ids=[card.evidence[0].block_id],
                         reason="离线模拟题干条件被错误设为必答。")
    client.app.state.ai.diagnose_answer=incomplete
    record=client.post("/api/material-imports",files={"file":("lecture.pdf",lecture_pdf())}).json()
    result=wait_import(client,record)
    cards=client.get("/api/state").json()["cards"]
    assert not cards and result["status"] == "failed"
    pending=result["job"]["rejected_cards"]
    assert len(pending)==2 and all(c["draft_preview"] for c in pending)
    assert all(c["quality"]["issues"][0]["code"] == "reference_answer_mismatch" for c in pending)
    assert "autcard-" not in client.get("/api/export").text


def test_automatic_budget_repeats_only_for_new_knowledge_and_resumes_by_pass(client):
    ai=client.app.state.ai
    calls=[]
    async def extract(blocks,ids,images,pages,**kwargs):
        calls.append(kwargs.get("avoid_knowledge",[]))
        offset=6 if calls[-1] else 0
        block=blocks[0]
        # Distinct source substrings exercise allocation and persistence, not semantic grading.
        return KnowledgeBatch(points=[KnowledgePoint(knowledge_id=f"k{n}",concept=f"fact{n}",statement=block.text[n:n+18],
                                  evidence=[{"block_id":block.block_id,"quote":block.text[n:n+18]}])
                                  for n in range(offset,offset+(2 if offset else 6))])
    ai.extract_knowledge_visual_partial=extract
    record=client.post("/api/material-imports",data={"count":"8"},files={"file":("lecture.pdf",lecture_pdf(1))}).json()
    result=wait_import(client,record)
    assert result["status"]=="completed",result
    assert len(result["job"]["card_ids"])==8 and len(calls)==2
    assert calls[0]==[] and len(calls[1])==6
    assert result["job"]["batch_summary"]["successful"]==2
    store=client.app.state.store
    job=store.get("auto_card_jobs",result["job_id"])
    job.update(status="interrupted",retryable=True)
    store.put("auto_card_jobs",job)
    client.post(f"/api/material-imports/{result['id']}/retry").raise_for_status()
    resumed=wait_import(client,result)
    assert resumed["job"]["card_ids"]==result["job"]["card_ids"] and len(calls)==2


def test_no_domain_knowledge_does_not_repeat_empty_requests_to_fill_a_large_target(client):
    calls=[]
    async def empty(*args):
        calls.append(True)
        return KnowledgeBatch(points=[])
    client.app.state.ai.extract_knowledge_visual=empty
    record=client.post("/api/material-imports",data={"count":"18"},files={"file":("outline.pdf",lecture_pdf(1))}).json()
    result=wait_import(client,record)
    assert result["status"]=="partial" and not result["retryable"]
    assert calls==[True] and not result["job"]["card_ids"]
    assert result["job"]["batch_summary"]["successful"]==3
