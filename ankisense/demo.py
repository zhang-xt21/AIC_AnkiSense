"""Original teaching fixtures; neither real course notes nor model output."""
from datetime import datetime, timezone

DEMO_ID = "demo-stacks-queues"
TEXTS = [
    "栈是一种只允许在同一端进行插入和删除的线性结构，该端称为栈顶。栈遵循后进先出（LIFO）：最后入栈的元素最先出栈。例如，A、B、C 依次入栈后，第一次出栈的是 C。",
    "队列是一种在队尾插入、在队头删除的线性结构。队列遵循先进先出（FIFO）：最先入队的元素最先出队。例如，A、B、C 依次入队后，第一次出队的是 A。",
    "执行出栈或出队前，需要检查结构是否为空。空栈没有可弹出的元素，空队列没有可移除的元素；对空结构直接执行删除会导致下溢。判空后应返回失败或按约定处理，不能编造一个数据元素。",
]


def seed_records() -> list[tuple[str, dict]]:
    now = datetime.now(timezone.utc).isoformat()
    records = [("documents", {"id": DEMO_ID, "title": "栈与队列 · 自编演示讲义", "filename": "自编演示讲义.txt", "is_demo": True, "created_at": now, "warning": "这三段内容为自编演示材料，不是实际课程讲义。页面编号为演示段落编号。", "page_count": 3})]
    for index, text in enumerate(TEXTS, 1):
        records.append(("blocks", {"block_id": f"demo-block-{index}", "document_id": DEMO_ID, "page": index, "text": text}))
    definitions = [
        ("stack", "栈的出入顺序规则是什么？", "栈遵循后进先出：最后入栈的元素最先出栈。", "栈", "basic", [("stack_lifo", "说明后进先出，或最后入栈的元素最先出栈")], 1),
        ("queue", "队列的出入顺序规则是什么？", "队列遵循先进先出：最先入队的元素最先出队。", "队列", "basic", [("queue_fifo", "说明先进先出，或最先入队的元素最先出队")], 2),
        ("empty", "空栈能直接执行出栈吗？为什么？", "不能。空栈没有可弹出的元素；出栈前应判空，避免下溢。", "边界条件", "basic", [("stack_empty", "指出空栈没有可弹出的元素，直接出栈会下溢"), ("stack_check", "说明出栈前应检查是否为空")], 3),
        ("stack-practice", "A、B、C 依次入栈后，第一次出栈的是谁？请说明理由。", "C，因为栈遵循后进先出，C 最后入栈。", "栈", "remediation", [("stack_lifo", "按后进先出规则指出 C 最先出栈")], 1),
        ("queue-practice", "A、B、C 依次入队后，第一次出队的是谁？请说明理由。", "A，因为队列遵循先进先出，A 最先入队。", "队列", "remediation", [("queue_fifo", "按先进先出规则指出 A 最先出队")], 2),
        ("empty-practice", "程序在弹出栈顶元素之前，应先检查什么？空栈时应如何处理？", "先检查栈是否为空；空栈应返回失败或按约定处理，不能直接弹出元素。", "边界条件", "remediation", [("stack_empty", "认识空栈无可弹出元素"), ("stack_check", "先判空，再返回失败或按约定处理")], 3),
    ]
    for slug, question, answer, concept, kind, points, page in definitions:
        records.append(("cards", {"id": f"demo-{slug}", "document_id": DEMO_ID, "question": question, "answer": answer, "concept": concept, "kind": kind, "rubric": [{"point_id": pid, "text": text} for pid, text in points], "evidence": [{"block_id": f"demo-block-{page}", "quote": TEXTS[page-1]}], "status": "draft", "version": 1, "is_demo": True, "created_at": now}))
    return records
