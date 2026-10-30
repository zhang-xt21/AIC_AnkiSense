"""Read-only summaries of current checkpoints, separate from historical issues."""
from collections import Counter


def deferred_split_count(batch):
    return sum(p.get("knowledge_id") in batch.get("split_parents", {})
               for p in batch.get("deferred_knowledge", []))


def summarize_batches(job):
    batches = job.get("batches", [])
    total = job.get("progress", {}).get("total", 0)
    statuses = Counter("partial" if batch.get("status") == "completed" and deferred_split_count(batch)
                       else batch.get("status", "pending") for batch in batches)
    success = statuses["completed"]
    active = job.get("status") in {"pending", "running"}
    failed, partial, running = statuses["failed"], statuses["partial"], statuses["running"] if active else 0
    summary = {"total": total, "successful": success, "failed": failed, "partial": partial,
               "running": running, "pending": max(0, total - success - failed - partial - running),
               "attempted": min(total, job.get("progress", {}).get("completed", 0)),
               "remaining": max(0, total - success),
               "pending_points": sum(max(0, len(batch.get("knowledge", [])) - len(batch.get("completed_knowledge_ids", [])))
                                     + len(batch.get("rejected_knowledge", [])) for batch in batches
                                     if batch.get("status") != "completed")
                                 + sum(deferred_split_count(batch) for batch in batches)}
    planned_hashes = {batch.get("batch_id") for batch in (job.get("plan") or {}).get("batches", []) if batch.get("batch_id")}
    def batch_key(batch):
        identifier = batch.get("batch_id")
        return identifier if identifier in planned_hashes else (tuple(batch.get("block_ids", [])), batch.get("pass_index", 0))
    planned = {batch_key(batch): batch.get("pages", [])
               for batch in (job.get("plan") or {}).get("batches", [])}
    details = []
    for batch in batches:
        deferred = deferred_split_count(batch)
        if batch.get("status") == "completed" and not deferred:
            continue
        pages = batch.get("pages") or planned.get(batch_key(batch), [])
        rejected = [*batch.get("rejected_knowledge", []), *batch.get("rejected_cards", [])]
        errors = [issue for issue in job.get("issues", []) if issue.get("code") in {"batch_failed", "card_failed", "knowledge_failed"}
                  and ((batch.get("batch_id") and issue.get("batch_id") == batch["batch_id"])
                       or (pages and issue.get("pages") == pages))]
        reasons = list(dict.fromkeys(item.get("reason") for item in rejected if item.get("reason")))
        if deferred:
            reasons.append(f"另有 {deferred} 个拆分目标待补做；本次数量预算不足时不会丢弃目标，请增加数量后另行制卡。")
        if not reasons and errors:
            reasons = [errors[-1].get("reason")]
        details.append({"batch_id": batch.get("batch_id"), "pages": pages,
                        "status": "partial" if deferred and batch.get("status") == "completed" else "pending" if not active and batch.get("status") == "running" else batch.get("status", "pending"),
                        "pending_points": max(0, len(batch.get("knowledge", [])) - len(batch.get("completed_knowledge_ids", [])))
                                          + len(batch.get("rejected_knowledge", [])) + deferred,
                        "reasons": reasons or ["等待继续处理。"]})
    saved = {batch_key(batch) for batch in batches}
    for identifiers, pages in planned.items():
        if identifiers not in saved:
            details.append({"batch_id": None, "pages": pages, "status": "pending", "pending_points": 0,
                            "reasons": ["此批尚未处理。"]})
    return summary, details
