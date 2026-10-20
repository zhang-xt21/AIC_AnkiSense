"""Unattended, checkpointed generation with card-scoped visual evidence."""
import asyncio
import hashlib
from uuid import uuid4

from fastapi import HTTPException

from .automatic_policy import POLICY, block_hash, content_hash
from .auto_cards import ACTIVE, batch_blocks, now
from .card_quality import rule_issues, validate_quality_report
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


async def run_automatic(service, job_id, ai):
    def check(blocks=()):
        service._check(job_id, blocks, signature)

    try:
        job = service._job(job_id)
        document, blocks, _, _, signature = service._capture(job["document_id"], True)
        if job.get("source_signature", signature) != signature:
            raise HTTPException(409, "讲义来源已变化，旧任务已停止；已生成卡牌仍保留。")
        skipped_pages = {}
        for page in sorted({b.page for b in blocks}):
            reason = automatic_page_skip("\n".join(b.text for b in blocks if b.page == page))
            if reason:
                skipped_pages[page] = reason
        blocks = [b for b in blocks if b.page not in skipped_pages]
        original = service.storage / "uploads" / f"{document['id']}.pdf"
        if not original.is_file():
            raise ValueError("讲义原页文件不存在，请重新导入。")
        pdf = await asyncio.to_thread(original.read_bytes)
        done = {b for batch in job["batches"] for b in batch["block_ids"]}
        batches = batch_blocks([b for b in blocks if b.block_id not in done])
        total = len(job["batches"]) + len(batches)
        initial_issues = [i for i in job["issues"] if i.get("stage") != "source" and i.get("code") != "source_unconfirmed" and not i.get("page")]
        initial_issues.extend({"code": "page_skipped", "severity": "info", "reason": reason, "pages": [page]} for page, reason in skipped_pages.items())
        initial_issues.extend({"code": "page_unreadable", "severity": "warning", "reason": "此页未识别到可用文字，其余页面继续处理。", "pages": [p["page"]]} for p in service.store.all("source_pages") if p["document_id"] == document["id"] and not p.get("text", "").strip() and p.get("status") != "empty")
        service._update(job_id, status="running", stage="knowledge", issues=initial_issues,
                        progress={"completed": len(job["batches"]), "total": total},
                        message="正在自动提取知识、对照原页检查并保存卡牌。")
        for focus in batches:
            check(focus)
            job = service._job(job_id)
            if len(job["card_ids"]) >= job["options"]["max_cards"]:
                service._update(job_id, issues=job["issues"] + [{"code": "card_limit", "reason": "已达到本次 600 张卡牌的处理上限。", "severity": "warning"}])
                break
            numbers = sorted({b.page for b in focus})
            records, issues, points, skipped = [], [], [], 0
            service._update(job_id, stage="knowledge", message=f"正在处理第 {numbers[0]}–{numbers[-1]} 页，卡牌会陆续显示。")
            try:
                images = [await asyncio.to_thread(render_page_png, pdf, page) for page in numbers]
                hashes = {str(p): hashlib.sha256(img).hexdigest() for p, img in zip(numbers, images)}
                points = (await retry_call(lambda: ai.extract_knowledge_visual(focus, [b.block_id for b in focus], images, numbers))).points
                check(focus)
                points = points[:job["options"]["max_cards"] - len(job["card_ids"])]
                for offset in range(0, len(points), 3):
                    generated = await retry_call(lambda: ai.generate_knowledge_cards(focus, points[offset:offset + 3]))
                    check(focus)
                    service._update(job_id, stage="quality", message=f"正在对照第 {numbers[0]}–{numbers[-1]} 页检查 {len(generated.cards)} 张卡牌。")
                    reports = await retry_call(lambda: ai.review_cards_visual([item.card for item in generated.cards], focus, images, numbers))
                    check(focus)
                    for item, report in zip(generated.cards, reports):
                        try:
                            card = item.card
                            validate_card(card, focus)
                            service._update(job_id, stage="quality", message=f"正在对照第 {numbers[0]}–{numbers[-1]} 页检查卡牌内容。")
                            validate_quality_report(report, card, focus)
                            check(focus)
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
                            existing = [c for c in service.store.all("cards") if c["document_id"] == document["id"] and not c.get("source_outdated") and c["status"] != "rejected"] + records
                            if rule_issues(card, focus, existing):
                                skipped += 1
                                continue
                            clear = report.verdict == "clear"
                            record = {**card.model_dump(), "id": "autcard-" + uuid4().hex,
                                      "document_id": document["id"], "version": 1, "status": "approved" if clear else "draft",
                                      "approval_method": "automatic" if clear else None, "is_demo": False,
                                      "created_at": now(), "auto_job_id": job_id,
                                      "knowledge_id": f"{job_id}:b{len(job['batches'])}:{item.knowledge_id}",
                                      "auto_quality": {**report.model_dump(), "scope": "text_and_original_page", "card_version": 1,
                                                       "source_block_ids": [b.block_id for b in focus], "prompt_version": AUTO_CARD_PROMPT_VERSION,
                                                       "repair_attempts": repairs, "checked_at": now()}}
                            cited = sorted({b.page for b in focus if b.block_id in {e.block_id for e in card.evidence}})
                            attachments = []
                            for page in (cited[:4] if job["options"]["image_mode"] != "off" else []):
                                cached = service.store.get("automatic_page_media", f"{document['id']}-p{page}")
                                revision = service.store.get("source_pages", f"{document['id']}-p{page}")["revision"]
                                if not cached or cached["revision"] != revision:
                                    normalized = await asyncio.to_thread(normalize_image, images[numbers.index(page)])
                                    media = save_media(service.store, service.storage, normalized,
                                                       source={"origin": "automatic_source_page", "document_id": document["id"], "page": page, "revision": revision}, check=lambda: check(focus))
                                    cached = {"id": f"{document['id']}-p{page}", "media_id": media["id"], "revision": revision}
                                    service.store.put("automatic_page_media", cached)
                                attachments.append({"media_id": cached["media_id"], "side": "answer", "caption": f"讲义原页 · 第 {page} 页"})
                            record.update(images=attachments, image_result={"status": "attached" if attachments else "skipped", "message": "答案侧附有引用的讲义原页。" if attachments else "本次未启用原页配图。"})
                            evidence_blocks = [b for b in focus if b.block_id in {e.block_id for e in card.evidence}]
                            if clear:
                                record["automatic_evidence"] = {"policy": POLICY, "visual_check": True,
                                    "card_hash": content_hash(record), "block_hash": block_hash(evidence_blocks),
                                    "page_images": hashes, "checked_at": now()}
                            records.append(record)
                            issues.extend({**i.model_dump(), "card_id": record["id"], "stage": "quality"} for i in report.issues)
                        except (AIError, ValueError) as error:
                            skipped += 1
                            issues.append({"code": "card_failed", "severity": "warning", "reason": error.message if isinstance(error, AIError) else "此卡牌未通过引用校验，已自动跳过。", "pages": numbers})
                if not points:
                    issues.append({"code": "no_knowledge", "severity": "info", "reason": "目录、标题或没有明确依据的内容已自动跳过。", "pages": numbers})
            except (AIError, ValueError) as error:
                issues.append({"code": "batch_failed", "severity": "warning", "reason": error.message if isinstance(error, AIError) else str(error), "pages": numbers})
            def commit():
                check(focus)
                current = service._job(job_id)
                checkpoint = {"block_ids": [b.block_id for b in focus], "card_ids": [c["id"] for c in records], "knowledge": [p.model_dump() for p in points]}
                current.update(batches=current["batches"] + [checkpoint], card_ids=current["card_ids"] + checkpoint["card_ids"],
                    knowledge_count=current["knowledge_count"] + len(points), skipped_count=current["skipped_count"] + skipped,
                    issues=current["issues"] + issues, progress={"completed": len(current["batches"]) + 1, "total": total}, updated_at=now())
                return [("cards", c) for c in records] + [("auto_card_jobs", current)]
            service.store.mutate(commit)
        check()
        final = service._job(job_id)
        count = len(final["card_ids"])
        failures = any(i.get("code") in {"batch_failed", "card_failed"} for i in final["issues"])
        service._update(job_id, status="completed" if count or not failures else "failed", stage="complete",
            error="模型处理未成功，任务详情中保留了失败原因。" if not count and failures else None,
            message=f"已生成 {count} 张卡牌，可直接查看。" + ("部分内容未处理成功，已保留说明。" if failures else ""))
    except asyncio.CancelledError:
        current = service._job(job_id)
        if current["status"] in ACTIVE:
            current.update(status="pending", message="等待服务恢复后自动继续。", updated_at=now())
            service.store.put("auto_card_jobs", current)
    except Exception as error:
        current = service._job(job_id)
        if current["status"] in ACTIVE:
            message = error.message if isinstance(error, AIError) else error.detail if isinstance(error, HTTPException) else "自动处理未完成，已保存的卡牌仍可查看。"
            current.update(status="outdated" if isinstance(error, HTTPException) and error.status_code == 409 else "failed", error=message, message=message, updated_at=now())
            service.store.put("auto_card_jobs", current)
