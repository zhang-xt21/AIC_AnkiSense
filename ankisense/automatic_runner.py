"""Unattended visual generation with resumable, candidate-scoped checkpoints."""
import asyncio
import hashlib

from fastapi import HTTPException

from .automatic_policy import POLICY, block_hash, content_hash
from .auto_cards import ACTIVE, automatic_plan, batch_blocks, digest, now
from .auto_card_models import KnowledgePoint
from .card_quality import rule_issues, validate_quality_report
from .draft_validation import select_knowledge_drafts
from .evidence import validate_card
from .llm import AIError
from .ingest import automatic_page_skip
from .media import normalize_image, save_media
from .ocr import render_page_png
from .prompts import AUTO_CARD_PROMPT_VERSION
from .schemas import SourceBlock


async def retry_call(operation):
    for attempt in range(2):
        try:
            return await operation()
        except AIError:
            if attempt:
                raise
            await asyncio.sleep(.5)


def _batch_id(focus):
    return digest([[block.block_id, block.text] for block in focus])[:24]


def _checkpoint(job, focus, numbers):
    identifier = _batch_id(focus)
    for saved in job["batches"]:
        if saved.get("batch_id") == identifier:
            return dict(saved)
        if not saved.get("batch_id") and saved["block_ids"] == [block.block_id for block in focus]:
            # Old runners checkpointed failures too. Do not treat those as done.
            failed = any(issue.get("code") in {"batch_failed", "card_failed"}
                         and issue.get("pages") == numbers for issue in job.get("issues", []))
            point_ids = [point["knowledge_id"] for point in saved.get("knowledge", [])]
            completed = [point_id for point_id in point_ids if any(
                str(card_id).endswith(":" + point_id) for card_id in saved.get("knowledge_ids", []))]
            return {**saved, "batch_id": identifier,
                    "status": "partial" if failed else "completed",
                    "completed_knowledge_ids": completed if failed else point_ids}
    return {"batch_id": identifier, "block_ids": [block.block_id for block in focus],
            "status": "pending", "knowledge": [], "card_ids": [], "completed_knowledge_ids": [],
            "rejected_cards": []}


def _save(service, job_id, focus, signature, checkpoint, records=(), issues=()):
    def commit():
        service._check(job_id, focus, signature)
        current = service._job(job_id)
        previous = current["batches"]
        batches = [batch for batch in previous if batch.get("batch_id") != checkpoint["batch_id"]
                   and not (not batch.get("batch_id") and batch["block_ids"] == checkpoint["block_ids"])]
        batches.append(checkpoint)
        rejected = [item for batch in batches for item in batch.get("rejected_cards", [])]
        current.update(batches=batches,
            card_ids=list(dict.fromkeys(current["card_ids"] + [card["id"] for card in records])),
            knowledge_count=sum(len(batch.get("knowledge", [])) for batch in batches),
            rejected_cards=rejected, rejected_count=len(rejected),
            issues=current["issues"] + list(issues), updated_at=now())
        current["progress"] = {"completed": sum(batch.get("status") in {"completed", "partial", "failed"} for batch in batches),
                               "total": current["progress"]["total"]}
        return [("cards", card) for card in records if not service.store.get("cards", card["id"])] + [("auto_card_jobs", current)]
    service.store.mutate(commit)


async def run_automatic(service, job_id, ai):
    def check(blocks=()):
        service._check(job_id, blocks, signature)

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
        plan = automatic_plan(blocks, job["options"].get("count"), job["options"]["max_cards"])
        all_batches = batch_blocks(blocks)
        groups = [(all_batches[batch["index"] - 1], batch["count"]) for batch in plan["batches"]]
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
        for focus, quota in groups:
            check(focus)
            job = service._job(job_id)
            numbers = sorted({block.page for block in focus})
            checkpoint = _checkpoint(job, focus, numbers)
            if checkpoint["status"] == "completed":
                _save(service, job_id, focus, signature, checkpoint)
                continue
            if len(job["card_ids"]) >= limit:
                service._update(job_id, issues=job["issues"] + [{"code": "card_limit", "reason": "已达到本次卡片数量上限，剩余原文未处理。", "severity": "warning"}])
                break
            checkpoint.update(status="running", rejected_cards=[])
            service._update(job_id, stage="knowledge", message=f"正在处理第 {numbers[0]}–{numbers[-1]} 页。")
            try:
                images = [await asyncio.to_thread(render_page_png, pdf, page) for page in numbers]
                hashes = {str(page): hashlib.sha256(image).hexdigest() for page, image in zip(numbers, images)}
                if checkpoint["knowledge"]:
                    points = [KnowledgePoint.model_validate(point) for point in checkpoint["knowledge"]]
                else:
                    points = (await retry_call(lambda: ai.extract_knowledge_visual(focus, [block.block_id for block in focus], images, numbers))).points
                    points = points[:min(quota or 6, limit - len(job["card_ids"]))]
                    checkpoint["knowledge"] = [point.model_dump() for point in points]
                    _save(service, job_id, focus, signature, checkpoint)
                finished = set(checkpoint.get("completed_knowledge_ids", []))
                # Migrate successful cards from older partial checkpoints by their stable suffix.
                for card_id in checkpoint.get("card_ids", []):
                    card = service.store.get("cards", card_id) or {}
                    for point in points:
                        if card.get("knowledge_id", "").endswith(":" + point.knowledge_id):
                            finished.add(point.knowledge_id)
                remaining = [point for point in points if point.knowledge_id not in finished]
                for offset in range(0, len(remaining), 3):
                    selected = remaining[offset:offset + 3]
                    records, issues = [], []
                    try:
                        generate = getattr(ai, "generate_knowledge_cards_partial", None) or ai.generate_knowledge_cards
                        generated = await retry_call(lambda: generate(focus, selected))
                        raw = [item.model_dump() if hasattr(item, "model_dump") else item for item in generated.cards]
                        selection = select_knowledge_drafts(raw, focus, selected)
                        rejected_by_point = {}
                        for rejected in list(getattr(generated, "rejected_cards", [])) + selection.rejected_cards:
                            rejected_by_point.setdefault(rejected.get("knowledge_id"), rejected)
                        rejected = list(rejected_by_point.values())
                        checkpoint["rejected_cards"].extend({**item, "batch_id": checkpoint["batch_id"], "pages": numbers} for item in rejected)
                        issues.extend({"code": "card_failed", "severity": "warning", "reason": item["reason"], "pages": numbers,
                                       "batch_id": checkpoint["batch_id"], "knowledge_id": item.get("knowledge_id")} for item in rejected)
                        cards = selection.cards
                        if cards:
                            service._update(job_id, stage="quality", message=f"正在对照原页检查 {len(cards)} 张候选卡片。")
                            review = getattr(ai, "review_cards_visual_semantic", None) or ai.review_cards_visual
                            reports = await retry_call(lambda: review([item.card for item in cards], focus, images, numbers))
                            if len(reports) != len(cards):
                                raise AIError("原页审核结果没有完整覆盖本批候选。")
                            for item, report in zip(cards, reports):
                                try:
                                    card = item.card
                                    validate_card(card, focus)
                                    validate_quality_report(report, card, focus)
                                    repairs = 0
                                    if report.issues:
                                        try:
                                            repaired = await retry_call(lambda: ai.repair_card(card, focus, report))
                                            validate_card(repaired, focus)
                                            checked = await retry_call(lambda: ai.review_card_visual(repaired, focus, images, numbers))
                                            validate_quality_report(checked, repaired, focus)
                                            card, report = repaired, checked
                                        except (AIError, ValueError):
                                            pass
                                        repairs = 1
                                    check(focus)
                                    existing = [saved for saved in service.store.all("cards") if saved["document_id"] == document["id"]
                                                and not saved.get("source_outdated") and saved["status"] != "rejected"] + records
                                    if rule_issues(card, focus, existing):
                                        finished.add(item.knowledge_id)
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
                                    checkpoint["card_ids"] = list(dict.fromkeys(checkpoint["card_ids"] + [record["id"]]))
                                    issues.extend({**issue.model_dump(), "card_id": record["id"], "stage": "quality"} for issue in report.issues)
                                except (AIError, ValueError) as error:
                                    issues.append({"code": "card_failed", "severity": "warning", "reason": str(error),
                                                   "pages": numbers, "knowledge_id": item.knowledge_id})
                        checkpoint["completed_knowledge_ids"] = sorted(finished)
                        _save(service, job_id, focus, signature, checkpoint, records, issues)
                    except (AIError, ValueError) as error:
                        _save(service, job_id, focus, signature, checkpoint, issues=[{
                            "code": "batch_failed", "severity": "warning", "reason": str(error), "pages": numbers,
                            "batch_id": checkpoint["batch_id"]}])
                checkpoint["status"] = "completed" if len(finished) == len(points) else "partial"
                if not points:
                    _save(service, job_id, focus, signature, checkpoint, issues=[{"code": "no_knowledge", "severity": "info",
                        "reason": "标题、目录或没有明确依据的内容已跳过。", "pages": numbers}])
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
        short = final["options"].get("count") is not None and count < final["options"]["count"]
        service._update(job_id, status=("partial" if count else "failed") if failures else "partial" if short else "completed", stage="complete",
            retryable=failures,
            error="部分内容未处理成功，可显式重试剩余候选。" if failures else None,
            message=f"已保存 {count} 张卡片。" + ("部分内容未完成，可重试补跑。" if failures else
                "原文中可独立制卡的有效知识不足或出现重复，实际数量少于目标；没有为凑数添加内容。" if short else ""))
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
