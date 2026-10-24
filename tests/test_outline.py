from ankisense.outline import coverage_report, duplicate_question, outline_topics
from test_generation_plan import MemoryStore, assert_limits
from ankisense.generation_plan import build_plan


def test_dense_outline_raises_recommendation_without_modifying_source():
    store = MemoryStore()
    text = "\n".join(f"{i}. 知识主题{i}的定义和适用条件\n这里说明这一个概念。" for i in range(1, 9))
    identifier = store.page(1, text)
    plan = build_plan(store, "doc")
    assert plan["recommended_count"] == 8
    assert len(plan["topics"]) == 8
    assert store.get("blocks", identifier)["text"] == text
    assert_limits(plan)
    for topic in plan["topics"]:
        assert text[topic["start"]:topic["end"]].strip().startswith(topic["title"])


def test_subpoints_are_discounted_and_repeated_titles_not_double_counted():
    store = MemoryStore()
    store.page(1, "1. 栈的定义\n①只允许在栈顶插入和删除元素。\n②元素遵循后进先出的顺序。")
    store.page(2, "1. 栈的定义\n①只允许在栈顶插入和删除元素。\n②元素遵循后进先出的顺序。")
    plan = build_plan(store, "doc")
    assert plan["recommended_count"] == 2
    assert len(plan["topics"]) == 3


def test_long_unstructured_paragraph_is_not_counted_as_many_outline_topics():
    store = MemoryStore()
    store.page(1, "普通叙述文字。" * 100)
    plan = build_plan(store, "doc")
    assert plan["topics"] == []
    assert plan["recommended_count"] <= 2


def test_same_source_and_long_answer_alone_do_not_claim_topic_coverage():
    blocks = [{"block_id": "b", "page": 1, "text": "1. 鸦片战争是近代史起点的原因\n2. 近代中国两大历史任务"}]
    topics = outline_topics(blocks)
    card = {"id": "c", "question": "为什么鸦片战争是近代史的起点？", "concept": "鸦片战争",
            "answer": blocks[0]["text"], "evidence": [{"block_id": "b"}]}
    report = coverage_report(topics, [card])
    assert report["matched_count"] == 1
    assert report["topics"][1]["status"] == "unmatched"
    assert coverage_report(topics, [card], stale=True)["matched_count"] == 0
    assert coverage_report(topics, [{**card, "source_outdated": True}])["matched_count"] == 0


def test_near_identical_questions_are_duplicates_but_distinct_questions_are_not():
    assert duplicate_question("队列的插入和删除分别在哪一端进行？", ["队列的插入和删除分别在哪一端进行？ "])
    assert not duplicate_question("队列遵循什么顺序？", ["队列在什么位置插入和删除？"])
    assert not duplicate_question("容量100的队列最多可以存多少个元素？", ["容量101的队列最多可以存多少个元素？"])
