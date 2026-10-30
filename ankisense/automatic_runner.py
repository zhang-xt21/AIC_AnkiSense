"""Unattended visual generation with resumable, candidate-scoped checkpoints."""
import asyncio
import hashlib
import json

from fastapi import HTTPException

from .automatic_policy import POLICY, block_hash, content_hash
from .auto_cards import ACTIVE, automatic_plan, automatic_batches, automatic_batch_id, batch_blocks, digest, now
from .auto_card_models import CardQualityReport, KnowledgePoint, QualityIssue
from .card_quality import rule_issues, validate_quality_report
from .draft_validation import select_knowledge_drafts, rejected_draft
from .evidence import validate_card
from .llm import AIError
from .ingest import automatic_page_skip
from .media import normalize_image, save_media
from .ocr import render_page_png
from .prompts import AUTO_CARD_PROMPT_VERSION
from .schemas import SourceBlock
from .learning_policy import balanced_points, non_learning_reason, unasked_expression_requirement, question_context_reason, multiple_target_reason
from .reference_check import reference_answer_passes, minimal_answer_passes
from .quote_recovery import recover_points, valid_supplement


SERVICE_ERRORS = {"connection", "timeout", "unavailable", "authentication", "configuration", "rate_limit"}
STOP_IMMEDIATELY = {"authentication", "configuration", "rate_limit"}


class ModelServiceStopped(RuntimeError):
    def __init__(self, error):
        self.error = error


async def retry_call(operation, on_failure):
    for attempt in range(2):
        try:
            return await operation()
        except AIError as error:
            on_failure(error)
            if attempt or error.code in STOP_IMMEDIATELY or error.code == "validation":
                raise
            await asyncio.sleep(.5)


def _batch_id(focus, pass_index=0):
    return automatic_batch_id(focus, pass_index)


def _checkpoint(job, focus, numbers, pass_index=0):
    identifier = _batch_id(focus, pass_index)
    for saved in job["batches"]:
        if saved.get("batch_id") == identifier:
            return dict(saved)
        if not pass_index and not saved.get("batch_id") and saved["block_ids"] == [block.block_id for block in focus]:
            # Old runners checkpointed failures too. Do not treat those as done.
            failed = any(issue.get("code") in {"batch_failed", "card_failed"}
                         and issue.get("pages") == numbers for issue in job.get("issues", []))
            point_ids = [point["knowledge_id"] for point in saved.get("knowledge", [])]
            completed = [point_id for point_id in point_ids if any(
                str(card_id).endswith(":" + point_id) for card_id in saved.get("knowledge_ids", []))]
            return {**saved, "batch_id": identifier,
                    "status": "partial" if failed else "completed",
                    "completed_knowledge_ids": completed if failed else point_ids}
    return {"batch_id": identifier, "source_batch_id": _batch_id(focus), "pass_index": pass_index,
            "block_ids": [block.block_id for block in focus], "pages": numbers,
            "status": "pending", "knowledge": [], "card_ids": [], "completed_knowledge_ids": [],
            "rejected_cards": [], "rejected_knowledge": [], "pending_cards": {}}


def _save(service, job_id, focus, signature, checkpoint, records=(), issues=()):
    def commit():
        service._check(job_id, focus, signature)
        current = service._job(job_id)
        previous = current["batches"]
        batches = [batch for batch in previous if batch.get("batch_id") != checkpoint["batch_id"]
                   and not (not checkpoint.get("pass_index") and not batch.get("batch_id") and batch["block_ids"] == checkpoint["block_ids"])]
        batches.append(checkpoint)
        rejected = [item for batch in batches for item in batch.get("rejected_cards", [])]
        rejected_knowledge = [item for batch in batches for item in batch.get("rejected_knowledge", [])]
        filtered = [item for batch in batches for item in batch.get("filtered_knowledge", [])]
        card_ids = list(dict.fromkeys(current["card_ids"] + [card["id"] for card in records]))
        saved_cards = {identifier: service.store.get("cards", identifier) for identifier in card_ids}
        saved_cards.update({card["id"]: card for card in records})
        page_map = {block["block_id"]: block["page"] for block in service.store.all("blocks")
                    if block["document_id"] == current["document_id"]}
        cited = sorted({page_map[e["block_id"]] for card in saved_cards.values() if card
                        for e in card["evidence"] if e["block_id"] in page_map})
        selected_pages = (current.get("plan") or {}).get("selected_pages", [])
        current.update(batches=batches,
            card_ids=card_ids, filtered_knowledge=filtered,
            skipped_count=len(filtered) + sum(batch.get("duplicate_count", 0) for batch in batches),
            coverage={"planned_source_pages": selected_pages, "card_cited_pages": cited,
                      "uncited_source_pages": sorted(set(selected_pages) - set(cited)),
                      "candidate_knowledge_count": sum(len(batch.get("knowledge_candidates", batch.get("knowledge", []))) for batch in batches),
                      "selected_knowledge_count": sum(len(batch.get("knowledge", [])) for batch in batches)},
            knowledge_count=sum(len(batch.get("knowledge", [])) for batch in batches),
            rejected_cards=rejected, rejected_count=len(rejected),
            split_history=[item for batch in batches for item in batch.get("split_history", [])],
            rejected_knowledge=rejected_knowledge,
            issues=current["issues"] + list(issues), updated_at=now())
        current["progress"] = {"completed": sum(batch.get("status") in {"completed", "partial", "failed"} for batch in batches),
                               "total": current["progress"]["total"]}
        return [("cards", card) for card in records if not service.store.get("cards", card["id"])] + [("auto_card_jobs", current)]
    service.store.mutate(commit)


async def run_automatic(service, job_id, ai):
    consecutive_failures = 0
    attempted_in_run = {}
    split_roots_in_run = set()

    def check(blocks=()):
        service._check(job_id, blocks, signature)

    async def call_model(operation):
        nonlocal consecutive_failures
        check()
        def failed(error):
            nonlocal consecutive_failures
            if error.code in SERVICE_ERRORS:
                consecutive_failures += 1
                if error.code in STOP_IMMEDIATELY or consecutive_failures >= 3:
                    raise ModelServiceStopped(error) from None
            else:
                consecutive_failures = 0
        result = await retry_call(operation, failed)
        consecutive_failures = 0
        check()
        return result

    async def check_reference(card, report, focus):
        if report.verdict != "clear":
            return report
        values = report.model_dump()
        reason = non_learning_reason(card.concept, card.question)
        code = "irrelevant_content"
        if not reason:
            reason = multiple_target_reason(card)
            code = "multiple_concepts"
        if not reason:
            reason = question_context_reason(card)
            code = "ambiguous_question"
        if not reason:
            reason = unasked_expression_requirement(card)
            code = "minimal_answer_mismatch"
        if not reason:
            code = "reference_answer_mismatch"
            try:
                diagnosis = await call_model(lambda: ai.diagnose_answer(card, focus, card.answer))
                values["reference_answer_check"] = diagnosis.model_dump()
                if reference_answer_passes(diagnosis, card, focus):
                    code = "minimal_answer_mismatch"
                    answer = await call_model(lambda: ai.minimal_answer_for_question(card, focus))
                    minimal = await call_model(lambda: ai.diagnose_answer(card, focus, answer))
                    values["minimal_answer_check"] = {"answer": answer, "diagnosis": minimal.model_dump()}
                    if minimal_answer_passes(values["minimal_answer_check"], card, focus):
                        return CardQualityReport.model_validate(values)
                    reason = "独立最短答案未满足评分点；题目范围或评分点可能多要求，需重新审核。"
                else:
                    reason = "参考答案自测未满足全部评分点；请核对题干已给条件、参考答案和必答评分点是否一致。"
            except AIError:
                reason = "答案一致性或独立最短答案检查未完成，不能自动放行；请重新检查。"
        values.update(verdict="needs_review", issues=[QualityIssue(code=code, severity="warning", reason=reason,
                      suggestion="保留题干适用条件，仅对题目要求的待答内容评分。").model_dump()])
        return CardQualityReport.model_validate(values)

    try:
        job = service._job(job_id)
        document, blocks, _, _, signature = service._capture(job["document_id"], True)
        if not service._matches_signature(job, signature):
            raise HTTPException(409, "讲义来源已变化，旧任务已停止；已生成卡片仍保留。")
        skipped_pages = {page: reason for page in sorted({block.page for block in blocks})
                         if (reason := automatic_page_skip("\n".join(block.text for block in blocks if block.page == page)))}
        blocks = [block for block in blocks if block.page not in skipped_pages]
        original = service.storage / "uploads" / f"{document['id']}.pdf"
        if not original.is_file():
            raise ValueError("讲义原页文件不存在，请重新导入。")
        pdf = await asyncio.to_thread(original.read_bytes)
        pdf_hash = hashlib.sha256(pdf).hexdigest()
        if document.get("pdf_sha256") and document["pdf_sha256"] != pdf_hash:
            raise HTTPException(409, "原始讲义文件已变化，请重新导入。")
        legacy = bool(job["batches"]) and (job.get("plan") or {}).get("strategy") != "page-balanced-v2"
        plan = job["plan"] if legacy else automatic_plan(blocks, job["options"].get("count"), job["options"]["max_cards"])
        all_batches = batch_blocks(blocks) if legacy else automatic_batches(blocks, job["options"].get("count"))
        groups = [(all_batches[batch["index"] - 1], batch["count"], batch.get("pass_index", 0)) for batch in plan["batches"]]
        initial_issues = [issue for issue in job["issues"] if issue.get("stage") != "source"
                          and issue.get("code") != "source_unconfirmed"]
        initial_issues.extend({"code": "page_skipped", "severity": "info", "reason": reason, "pages": [page]}
                              for page, reason in skipped_pages.items() if not any(
                                  issue.get("code") == "page_skipped" and issue.get("pages") == [page] for issue in initial_issues))
        initial_issues.extend({"code": "page_unreadable", "severity": "warning", "reason": "本页无可用文字，其余页面继续处理。", "pages": [page["page"]]}
                              for page in service.store.all("source_pages") if page["document_id"] == document["id"]
                              and (page.get("status") == "failed" or not page.get("text", "").strip()) and page.get("status") != "empty")
        service._update(job_id, status="running", stage="knowledge", issues=initial_issues, plan=plan,
                        source_signature=signature,
                        progress={"completed": 0, "total": len(groups)},
                        message="正在提取知识、对照原页检查并陆续保存卡片。")
        limit = min(job["options"]["max_cards"], job["options"].get("count") or 600)
        # Only after all planned pages have their initial opportunity, reuse
        # validated deferred knowledge. Do not re-extract or retry failed cards.
        groups.append((None, None, None))
        for focus, quota, pass_index in groups:
            if focus is None:
                appended = False
                current = service._job(job_id)
                room = max(0, limit - sum(len(b.get("knowledge", [])) + len(b.get("rejected_knowledge", []))
                                          for b in current["batches"]))
                known = {(p["concept"], p["statement"]) for b in current["batches"] for p in b.get("knowledge", [])}
                for saved in list(current["batches"]):
                    if room <= 0:
                        break
                    deferred = saved.get("deferred_knowledge", [])
                    split_ids = saved.get("split_parents", {})
                    if not deferred or (saved.get("status") != "completed" and not (
                            saved.get("status") == "partial" and any(p["knowledge_id"] in split_ids for p in deferred))):
                        continue
                    focus_blocks = [block for block in blocks if block.block_id in saved["block_ids"]]
                    check(focus_blocks)
                    eligible = [KnowledgePoint.model_validate(p) for p in deferred
                                if (p["concept"], p["statement"]) not in known
                                and (saved.get("status") == "completed" or p["knowledge_id"] in split_ids)]
                    chosen = balanced_points(eligible, focus_blocks, room)
                    if not chosen:
                        continue
                    chosen_ids = {p.knowledge_id for p in chosen}
                    updated = {**saved, "status": "pending",
                               "knowledge": saved["knowledge"] + [p.model_dump() for p in chosen],
                               "deferred_knowledge": [p for p in deferred if p["knowledge_id"] not in chosen_ids],
                               "backfilled_count": saved.get("backfilled_count", 0) + len(chosen)}
                    _save(service, job_id, focus_blocks, signature, updated)
                    groups.append((focus_blocks, None, saved.get("pass_index", 0)))
                    appended = True
                    room -= len(chosen)
                    known.update((p.concept, p.statement) for p in chosen)
                if appended:
                    groups.append((None, None, None))
                continue
            check(focus)
            job = service._job(job_id)
            numbers = sorted({block.page for block in focus})
            checkpoint = _checkpoint(job, focus, numbers, pass_index)
            if checkpoint["status"] == "completed":
                _save(service, job_id, focus, signature, checkpoint)
                continue
            if len(job["card_ids"]) >= limit:
                service._update(job_id, issues=job["issues"] + [{"code": "card_limit", "reason": "已达到本次卡片数量上限，剩余原文未处理。", "severity": "warning"}])
                break
            exhausted = any((saved.get("source_batch_id") or saved.get("batch_id")) == _batch_id(focus)
                            and saved.get("knowledge_extracted") and saved.get("status") == "completed"
                            and (not saved.get("knowledge") or saved.get("duplicate_count", 0) == len(saved["knowledge"]))
                            for saved in job["batches"])
            if pass_index and exhausted:
                checkpoint.update(status="completed", knowledge_extracted=True, exhausted=True)
                _save(service, job_id, focus, signature, checkpoint)
                continue
            previous_rejected = list(checkpoint.get("rejected_cards", []))
            checkpoint.update(status="running", pages=numbers)
            checkpoint.setdefault("pending_cards", {})
            service._update(job_id, stage="knowledge", message=f"正在处理第 {numbers[0]}–{numbers[-1]} 页。")
            try:
                images = [await asyncio.to_thread(render_page_png, pdf, page) for page in numbers]
                hashes = {str(page): hashlib.sha256(image).hexdigest() for page, image in zip(numbers, images)}
                points = [KnowledgePoint.model_validate(point) for point in checkpoint["knowledge"]]
                invalid_points = checkpoint.get("rejected_knowledge", [])
                # Restore source-bound supplements when resuming an existing batch.
                for point in points:
                    for evidence in point.evidence:
                        if evidence.block_id not in {b.block_id for b in focus}:
                            saved = service.store.get("blocks", evidence.block_id)
                            if saved:
                                block = SourceBlock.model_validate(saved)
                                page = service.store.get("source_pages", f"{block.document_id}-p{block.page}")
                                if valid_supplement(service.store, block, page):
                                    focus = [*focus, block]
                if invalid_points:
                    fixed, unresolved, extra = await recover_points(service.store, pdf, focus, invalid_points,
                                                                   ai, check, job_id)
                    points += [point for point in fixed if point.knowledge_id not in {p.knowledge_id for p in points}]
                    invalid_points = unresolved
                    focus = list({b.block_id: b for b in [*focus, *extra]}.values())
                    checkpoint["knowledge"] = [p.model_dump() for p in points]
                    checkpoint["rejected_knowledge"] = invalid_points
                focus_ids = [block.block_id for block in focus]
                if invalid_points or not (points or checkpoint.get("knowledge_extracted")):
                    repair_points = getattr(ai, "repair_knowledge_points_visual", None)
                    extract_partial = getattr(ai, "extract_knowledge_visual_partial", None)
                    extract = extract_partial or ai.extract_knowledge_visual
                    if invalid_points and repair_points:
                        extracted = await call_model(lambda: repair_points(focus, focus_ids, images, numbers, invalid_points))
                    else:
                        previous = [point for saved in job["batches"]
                                    if saved.get("batch_id") != checkpoint["batch_id"]
                                    and saved.get("block_ids") == focus_ids for point in saved.get("knowledge", [])]
                        avoid = [{"concept": point["concept"], "statement": point["statement"][:300]} for point in previous]
                        kwargs = {"avoid_knowledge": avoid} if pass_index and avoid and extract_partial else {}
                        extracted = await call_model(lambda: extract(focus, focus_ids, images, numbers, **kwargs))
                    candidates = list(extracted.points)
                    rejected_points = list(getattr(extracted, "rejected_points", []))
                    if rejected_points:
                        fixed, rejected_points, extra = await recover_points(service.store, pdf, focus,
                                                                             rejected_points, ai, check, job_id)
                        candidates.extend(fixed)
                        focus = list({b.block_id: b for b in [*focus, *extra]}.values())
                    filtered = list(getattr(extracted, "filtered_points", []))
                    eligible = []
                    for point in candidates:
                        reason = non_learning_reason(point.concept, point.statement)
                        if reason:
                            filtered.append({"knowledge_id": point.knowledge_id, "reason": reason, "point": point.model_dump()})
                        else:
                            eligible.append(point)
                    candidates = eligible
                    checkpoint["filtered_knowledge"] = checkpoint.get("filtered_knowledge", []) + filtered
                    checkpoint["knowledge_candidates"] = [point.model_dump() for point in candidates]
                    if invalid_points:
                        expected = {item["knowledge_id"] for item in invalid_points}
                        accepted = {point.knowledge_id for point in points}
                        repaired_ids = set()
                        for point in candidates:
                            if point.knowledge_id in expected and point.knowledge_id not in accepted:
                                points.append(point)
                                accepted.add(point.knowledge_id)
                                repaired_ids.add(point.knowledge_id)
                        failures = {item["knowledge_id"]: item for item in rejected_points}
                        filtered_ids = {item["knowledge_id"] for item in filtered}
                        invalid_points = [failures.get(item["knowledge_id"], item) for item in invalid_points
                                          if item["knowledge_id"] not in repaired_ids | filtered_ids]
                    else:
                        capacity = min(quota or 6, limit - len(job["card_ids"]))
                        points = balanced_points(candidates, focus, capacity)
                        selected_ids = {point.knowledge_id for point in points}
                        checkpoint["deferred_knowledge"] = [point.model_dump() for point in candidates if point.knowledge_id not in selected_ids]
                        invalid_points = rejected_points[:max(0, capacity - len(points))]
                    checkpoint["knowledge"] = [point.model_dump() for point in points]
                    checkpoint["knowledge_extracted"] = True
                    checkpoint["rejected_knowledge"] = invalid_points
                    _save(service, job_id, focus, signature, checkpoint, issues=[{
                        "code": "knowledge_failed", "severity": "warning", "stage": "knowledge",
                        "reason": item["reason"], "pages": numbers, "batch_id": checkpoint["batch_id"],
                        "knowledge_id": item["knowledge_id"]} for item in invalid_points])
                finished = set(checkpoint.get("completed_knowledge_ids", []))
                # Migrate successful cards from older partial checkpoints by their stable suffix.
                for card_id in checkpoint.get("card_ids", []):
                    card = service.store.get("cards", card_id) or {}
                    for point in points:
                        if card.get("knowledge_id", "").endswith(":" + point.knowledge_id):
                            finished.add(point.knowledge_id)
                seen = attempted_in_run.setdefault(checkpoint["batch_id"], set())
                remaining = [point for point in points if point.knowledge_id not in finished and point.knowledge_id not in seen]
                for offset in range(0, len(remaining), 3):
                    selected = remaining[offset:offset + 3]
                    seen.update(point.knowledge_id for point in selected)
                    selected_ids = {point.knowledge_id for point in selected}
                    checkpoint["rejected_cards"] = [item for item in checkpoint.get("rejected_cards", [])
                                                    if item.get("knowledge_id") and item["knowledge_id"] not in selected_ids]
                    records, issues = [], []
                    try:
                        cached = dict(checkpoint["pending_cards"])
                        for old in previous_rejected:
                            identifier = old.get("knowledge_id")
                            if identifier and identifier not in cached and not old.get("preview_truncated"):
                                try:
                                    cached[identifier] = {"knowledge_id": identifier, "card": json.loads(old["draft_preview"])}
                                except (KeyError, ValueError, TypeError):
                                    pass
                        cached_selection = select_knowledge_drafts(
                            [cached[point.knowledge_id] for point in selected if point.knowledge_id in cached], focus, selected)
                        raw = [item.model_dump() for item in cached_selection.cards]
                        cached_ids = {item.knowledge_id for item in cached_selection.cards}
                        to_generate = [point for point in selected if point.knowledge_id not in cached_ids]
                        generated_rejections = []
                        if to_generate:
                            feedback = [item for item in previous_rejected
                                        if item.get("knowledge_id") in {point.knowledge_id for point in to_generate}]
                            repair_candidates = getattr(ai, "repair_knowledge_cards_partial", None)
                            generate = getattr(ai, "generate_knowledge_cards_partial", None) or ai.generate_knowledge_cards
                            if feedback and repair_candidates:
                                generated = await call_model(lambda: repair_candidates(focus, to_generate, feedback))
                            else:
                                generated = await call_model(lambda: generate(focus, to_generate))
                            raw.extend(item.model_dump() if hasattr(item, "model_dump") else item for item in generated.cards)
                            generated_rejections = list(getattr(generated, "rejected_cards", []))
                        selection = select_knowledge_drafts(raw, focus, selected)
                        rejected_by_point = {}
                        for rejected in generated_rejections + selection.rejected_cards:
                            rejected_by_point.setdefault(rejected.get("knowledge_id"), rejected)
                        rejected = list(rejected_by_point.values())
                        checkpoint["rejected_cards"].extend({**item, "batch_id": checkpoint["batch_id"], "pages": numbers} for item in rejected)
                        issues.extend({"code": "card_failed", "severity": "warning", "reason": item["reason"], "pages": numbers,
                                       "batch_id": checkpoint["batch_id"], "knowledge_id": item.get("knowledge_id")} for item in rejected)
                        cards = selection.cards
                        if cards:
                            checkpoint["pending_cards"].update({item.knowledge_id: item.model_dump() for item in cards})
                            _save(service, job_id, focus, signature, checkpoint, issues=issues)
                            issues = []
                            service._update(job_id, stage="quality", message=f"正在对照原页检查 {len(cards)} 张候选卡片。")
                            review = getattr(ai, "review_cards_visual_semantic", None) or ai.review_cards_visual
                            reports = await call_model(lambda: review([item.card for item in cards], focus, images, numbers))
                            if len(reports) != len(cards):
                                raise AIError("原页审核结果没有完整覆盖本批候选。")
                            for item, report in zip(cards, reports):
                                try:
                                    card = item.card
                                    validate_card(card, focus)
                                    validate_quality_report(report, card, focus)
                                    report = await check_reference(card, report, focus)
                                    initial_quality = report.model_dump()
                                    repairs = 0
                                    # Split before shortening: all child goals are retained.
                                    # Children get one normal repair, but cannot split recursively.
                                    split = getattr(ai, "split_overloaded_knowledge", None)
                                    split_parents = checkpoint.setdefault("split_parents", {})
                                    attempted = checkpoint.setdefault("split_attempted_ids", [])
                                    root_target = item.knowledge_id
                                    lineage = set()
                                    while root_target in split_parents and root_target not in lineage:
                                        lineage.add(root_target)
                                        root_target = split_parents[root_target]
                                    root_key = (checkpoint["batch_id"], root_target)
                                    if (split and any(i.code == "multiple_concepts" for i in report.issues)
                                            and root_key not in split_roots_in_run):
                                        split_roots_in_run.add(root_key)
                                        if item.knowledge_id not in attempted:
                                            attempted.append(item.knowledge_id)
                                        _save(service, job_id, focus, signature, checkpoint, records, issues)
                                        issues = []
                                        try:
                                            original = next(p for p in points if p.knowledge_id == item.knowledge_id)
                                            divided = await call_model(lambda: split(original, card, focus, report, images, numbers))
                                            from .card_quality import validate_knowledge_points
                                            validate_knowledge_points(divided.points, focus)
                                            if not 2 <= len(divided.points) <= 6:
                                                raise ValueError("过载目标须拆成2至6个独立目标。")
                                            scope = (report.review_checks or {}).get("learning_scope") or {}
                                            if scope.get("verdict") == "multiple" and len(divided.points) < len(scope.get("targets", [])):
                                                raise ValueError("拆分未保留全部已识别目标。")
                                            children = [p.model_copy(update={"knowledge_id": "split-" + digest([item.knowledge_id, p.concept, p.statement])[:32]}) for p in divided.points]
                                            if len({p.knowledge_id for p in children}) != len(children):
                                                raise ValueError("拆分产生重复目标。")
                                            checkpoint.setdefault("split_history", []).append({"parent": original.model_dump(),
                                                "card": card.model_dump(), "quality": report.model_dump(),
                                                "children": [p.model_dump() for p in children], "at": now()})
                                            split_parents.update({p.knowledge_id: item.knowledge_id for p in children})
                                            checkpoint["deferred_knowledge"] = checkpoint.get("deferred_knowledge", []) + [p.model_dump() for p in children]
                                            points = [p for p in points if p.knowledge_id != item.knowledge_id]
                                            checkpoint["knowledge"] = [p.model_dump() for p in points]
                                            checkpoint["pending_cards"].pop(item.knowledge_id, None)
                                            # The final queue fills these goals only within the card cap.
                                            continue
                                        except (AIError, ValueError):
                                            # Do not shorten the parent and silently lose its other goals.
                                            checkpoint.setdefault("rework_history", []).append({"kind": "split_failed",
                                                "knowledge_id": item.knowledge_id, "before": card.model_dump(),
                                                "quality": initial_quality, "at": now()})
                                            checkpoint["rejected_cards"].append({**rejected_draft(card.model_dump(), 1, "过载目标拆分失败，原卡及各目标已保留待处理。"),
                                                "knowledge_id": item.knowledge_id, "batch_id": checkpoint["batch_id"], "pages": numbers,
                                                "quality": report.model_dump(), "split_attempts": 1})
                                            continue
                                    if report.issues and not any(issue.code in {"review_protocol_error", "multiple_concepts"} for issue in report.issues):
                                        try:
                                            repaired = await call_model(lambda: ai.repair_card(card, focus, report))
                                            validate_card(repaired, focus)
                                            checked = await call_model(lambda: ai.review_card_visual(repaired, focus, images, numbers))
                                            validate_quality_report(checked, repaired, focus)
                                            checked = await check_reference(repaired, checked, focus)
                                            card, report = repaired, checked
                                        except (AIError, ValueError):
                                            pass
                                        repairs = 1
                                    if repairs or report.verdict != "clear":
                                        checkpoint.setdefault("rework_history", []).append({"kind": "repair" if repairs else "review_failed",
                                            "knowledge_id": item.knowledge_id, "before": item.card.model_dump(),
                                            "after": card.model_dump(), "initial_quality": initial_quality,
                                            "final_quality": report.model_dump(), "at": now()})
                                    if report.verdict != "clear":
                                        reason = "；".join(issue.reason for issue in report.issues) or "自动重做后仍未通过检查。"
                                        checkpoint["pending_cards"][item.knowledge_id] = {"knowledge_id": item.knowledge_id, "card": card.model_dump()}
                                        checkpoint["rejected_cards"].append({**rejected_draft(card.model_dump(), 1, reason),
                                            "knowledge_id": item.knowledge_id, "batch_id": checkpoint["batch_id"], "pages": numbers,
                                            "quality": report.model_dump(), "repair_attempts": repairs})
                                        issues.append({"code": "card_failed", "severity": "warning", "reason": reason,
                                            "pages": numbers, "batch_id": checkpoint["batch_id"], "knowledge_id": item.knowledge_id})
                                        continue
                                    check(focus)
                                    existing = [saved for saved in service.store.all("cards") if saved["document_id"] == document["id"]
                                                and not saved.get("source_outdated") and saved["status"] != "rejected"] + records
                                    if rule_issues(card, focus, existing):
                                        checkpoint["duplicate_count"] = checkpoint.get("duplicate_count", 0) + 1
                                        finished.add(item.knowledge_id)
                                        checkpoint["pending_cards"].pop(item.knowledge_id, None)
                                        continue
                                    clear = report.verdict == "clear"
                                    identifier = "autcard-" + digest([job_id, checkpoint["batch_id"], item.knowledge_id])[:32]
                                    record = {**card.model_dump(), "id": identifier, "document_id": document["id"], "version": 1,
                                        "status": "approved" if clear else "draft", "approval_method": "automatic" if clear else None,
                                        "is_demo": False, "created_at": now(), "auto_job_id": job_id,
                                        "knowledge_id": f"{job_id}:{checkpoint['batch_id']}:{item.knowledge_id}",
                                        "auto_quality": {**report.model_dump(), "scope": "text_and_original_page", "card_version": 1,
                                            "source_block_ids": [block.block_id for block in focus], "prompt_version": AUTO_CARD_PROMPT_VERSION,
                                            "repair_attempts": repairs, "checked_at": now()}}
                                    cited = sorted({block.page for block in focus if block.block_id in {entry.block_id for entry in card.evidence}})
                                    attachments = []
                                    for page in (cited[:4] if job["options"]["image_mode"] != "off" else []):
                                        current_page = service.store.get("source_pages", f"{document['id']}-p{page}")
                                        revision = current_page["revision"]
                                        cache_id = f"{document['id']}-p{page}"
                                        cached = service.store.get("automatic_page_media", cache_id)
                                        if not cached or cached["revision"] != revision:
                                            normalized = await asyncio.to_thread(normalize_image, images[numbers.index(page)])
                                            media = save_media(service.store, service.storage, normalized,
                                                source={"origin": "automatic_source_page", "document_id": document["id"], "page": page,
                                                        "revision": revision, "pdf_hash": pdf_hash}, check=lambda: check(focus))
                                            cached = {"id": cache_id, "media_id": media["id"], "revision": revision}
                                            service.store.put("automatic_page_media", cached)
                                        attachments.append({"media_id": cached["media_id"], "side": "answer", "caption": f"讲义原页 · 第 {page} 页"})
                                    record.update(images=attachments, image_result={"status": "attached" if attachments else "skipped",
                                        "message": "答案侧附有引用的讲义原页。" if attachments else "本次未启用原页配图。"})
                                    evidence_blocks = [SourceBlock.model_validate(service.store.get("blocks", entry.block_id))
                                                       for entry in card.evidence]
                                    if clear:
                                        record["automatic_evidence"] = {"policy": POLICY, "visual_check": True,
                                            "card_hash": content_hash(record), "block_hash": block_hash(evidence_blocks),
                                            "page_images": hashes, "pdf_sha256": pdf_hash, "checked_at": now()}
                                    records.append(record)
                                    finished.add(item.knowledge_id)
                                    checkpoint["pending_cards"].pop(item.knowledge_id, None)
                                    checkpoint["card_ids"] = list(dict.fromkeys(checkpoint["card_ids"] + [record["id"]]))
                                    issues.extend({**issue.model_dump(), "card_id": record["id"], "stage": "quality"} for issue in report.issues)
                                except (AIError, ValueError) as error:
                                    checkpoint["rejected_cards"].append({
                                        **rejected_draft(item.card.model_dump(), 1, str(error)),
                                        "knowledge_id": item.knowledge_id, "batch_id": checkpoint["batch_id"], "pages": numbers})
                                    issues.append({"code": "card_failed", "severity": "warning", "reason": str(error),
                                                   "pages": numbers, "knowledge_id": item.knowledge_id})
                        checkpoint["completed_knowledge_ids"] = sorted(finished)
                        _save(service, job_id, focus, signature, checkpoint, records, issues)
                    except (AIError, ValueError) as error:
                        _save(service, job_id, focus, signature, checkpoint, issues=[{
                            "code": "batch_failed", "severity": "warning", "reason": str(error), "pages": numbers,
                            "batch_id": checkpoint["batch_id"]}])
                checkpoint["status"] = "completed" if len(finished) == len(points) and not invalid_points else "partial"
                if not points and not invalid_points and not checkpoint.get("split_history"):
                    _save(service, job_id, focus, signature, checkpoint, issues=[{"code": "no_knowledge", "severity": "info",
                        "reason": "标题、目录、课程管理或没有明确学科依据的内容已跳过。", "pages": numbers}])
                else:
                    _save(service, job_id, focus, signature, checkpoint)
            except (AIError, ValueError) as error:
                checkpoint["status"] = "failed"
                _save(service, job_id, focus, signature, checkpoint, issues=[{
                    "code": "batch_failed", "severity": "warning", "reason": str(error), "pages": numbers,
                    "batch_id": checkpoint["batch_id"]}])
        check()
        final = service._job(job_id)
        failures = any(batch.get("status") in {"failed", "partial", "running"} for batch in final["batches"])
        count = len(final["card_ids"])
        split_pending = sum(1 for batch in final["batches"] for p in batch.get("deferred_knowledge", [])
                            if p["knowledge_id"] in batch.get("split_parents", {}))
        short = final["options"].get("count") is not None and count < final["options"]["count"]
        service._update(job_id, status=("partial" if count else "failed") if failures else "partial" if short or split_pending else "completed", stage="complete",
            retryable=failures,
            error="部分内容未处理成功，可显式重试剩余候选。" if failures else None,
            message=f"已保存 {count} 张卡片。" + ("部分内容未完成，可重试补跑。" if failures else
                "本次提取和筛选后的候选少于目标；没有为凑数添加内容。" if short else "")
                + (f" 另有 {split_pending} 个拆分目标受本次数量上限限制，已保留待补做。" if split_pending else "")
                + f" 已筛除管理条目 {len(final.get('filtered_knowledge', []))} 项，去重 {sum(b.get('duplicate_count', 0) for b in final['batches'])} 张。")
    except ModelServiceStopped as stopped:
        # Model-service failure is separate from a bad page or invalid candidate.
        # Stop issuing requests, but keep all completed checkpoints for explicit retry.
        current = service._job(job_id)
        if current["status"] in ACTIVE:
            count = len(current["card_ids"])
            failure = {"code": stopped.error.code, "message": stopped.error.message,
                       "action": "检查模型设置、网络或额度后，再点击继续；仅处理未完成内容。"}
            current.update(status="partial" if count else "failed", retryable=True, failure=failure,
                           error=stopped.error.message,
                           message=f"模型服务异常，已停止后续请求；已保存 {count} 张卡片。", updated_at=now())
            service.store.put("auto_card_jobs", current)
    except asyncio.CancelledError:
        current = service._job(job_id)
        if current["status"] in ACTIVE:
            current.update(status="interrupted", retryable=True, message="任务已中断，点击继续后仅处理未完成内容。", updated_at=now())
            service.store.put("auto_card_jobs", current)
    except Exception as error:
        current = service._job(job_id)
        if current["status"] in ACTIVE:
            message = error.message if isinstance(error, AIError) else error.detail if isinstance(error, HTTPException) else "自动处理未完成，已保存卡片仍保留。"
            current.update(status="outdated" if isinstance(error, HTTPException) and error.status_code == 409 else "failed",
                           retryable=not (isinstance(error, HTTPException) and error.status_code == 409),
                           error=message, message=message, updated_at=now())
            service.store.put("auto_card_jobs", current)
