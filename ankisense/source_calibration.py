"""Original-page transcription, conservative adoption, and immutable evidence."""
import asyncio
from datetime import datetime, timezone
from difflib import SequenceMatcher
import re
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from .llm import AIError
from .ocr import recognize_region, render_page_png
from .ocr_calibration import analyze_page, _box
from .source_calibration_models import CalibrationRequest
from .source_policy import SOURCE_POLICY_VERSION, machine_verified, text_hash
from .sources import page_id, save_page_revision


def canonical(text):
    return re.sub(r"\s+", " ", text).strip()


def protected(text):
    patterns = (r"[+−-]?\d+(?:[.,]\d+)*|[零一二三四五六七八九十百千万亿两]+", r"[=+*/\-−<>≤≥≠±×÷∑√∫∞π⁰-⁹₀-₉α-ωΑ-Ω^]",
                r"不|无|非|未|禁止|\b(?:not|no|never|without|except)\b",
                r"%|％|℃|°[CF]|千克|毫克|毫米|厘米|千米|毫秒|毫升|摄氏|[微毫厘分千兆]?(?:安|欧|伏|帕|焦|瓦|米|克|升|秒)|吨|赫兹|字节|比特|位|[a-zA-Zμ]+")
    return [re.findall(pattern, text, re.I) for pattern in patterns]


def assess(page, first, second):
    """Only the local service may turn these policy results into source authority."""
    before, after = page.get("text", ""), first.text
    issues = [*first.uncertainties, *second.uncertainties]
    agree = canonical(after) == canonical(second.text)
    if not agree:
        issues.append("两次原页读取结果不一致，需确认差异。")
    if not before.strip() or not after.strip():
        issues.append("原识别或候选文字为空，需对照原页确认。")
    protected_same = protected(before) == protected(after)
    if not protected_same:
        issues.append("修正涉及数字、单位、否定、英文标识或公式符号，必须人工确认。")
    changes = [] if before == after else [{"start": i, "end": j, "before": before[i:j], "after": after[k:l],
                "candidate_start": k, "candidate_end": l}
               for op, i, j, k, l in SequenceMatcher(None, before, after, autojunk=False).get_opcodes() if op != "equal"]
    edited = sum(max(len(c["before"]), len(c["after"])) for c in changes)
    if len(changes) > 20 or edited / max(1, len(before)) > .08:
        issues.append("修改范围较大，不能自动采用。")
    screening = analyze_page(page)
    if any(i["code"] in {"reading_order", "critical_formula"} for i in screening["issues"]):
        issues.append("存在公式或多栏/表格阅读顺序风险，请人工确认。")
    return {"proposed_text": after, "changes": changes[:100], "issues": list(dict.fromkeys(issues)),
            "visual_reads_agree": agree, "protected_content_unchanged": protected_same}


def corroborate_changes(content, page, result, recognize=None):
    """Corroborate the changed whole line on the original image, never by score."""
    recognize = recognize or recognize_region
    if not result["changes"]:
        return True
    if page.get("text") != page.get("raw_text"):
        result["issues"].append("当前文字与原识别已不同，局部位置不能直接复用。")
        return False
    lines = page.get("ocr_lines") or []
    checks = {}
    for change in result["changes"]:
        line = next((line for line in lines if type(line.get("start")) is int and type(line.get("end")) is int
                     and line["start"] <= change["start"] and change["end"] <= line["end"]
                     and page["text"][line["start"]:line["end"]] == line.get("text") and _box(line.get("bbox"))), None)
        if not line:
            result["issues"].append("修改位置缺少可靠的原图定位，需人工确认。")
            return False
        key = (line["start"], line["end"])
        if key not in checks:
            if len(checks) >= 8:
                result["issues"].append("待复识别区域较多，请人工确认。")
                return False
            desired = line["text"]
            for item in reversed(result["changes"]):
                if line["start"] <= item["start"] and item["end"] <= line["end"]:
                    left, right = item["start"] - line["start"], item["end"] - line["start"]
                    desired = desired[:left] + item["after"] + desired[right:]
            box = line["bbox"]
            padded = [max(0, box[0] - .006), max(0, box[1] - .006), min(1, box[2] + .006), min(1, box[3] + .006)]
            local = recognize(content, page["page"], padded)
            checks[key] = canonical(local.get("text", "")) == canonical(desired)
        change["bbox"] = line["bbox"]
        change["local_supported"] = checks[key]
        if not checks[key]:
            result["issues"].append("局部复识别未支持修正后的整行文字，请人工确认。")
            return False
    return True


class SourceCalibrationService:
    def __init__(self, store, storage, ai, settings):
        self.store, self.storage, self.ai, self.settings = store, storage, ai, settings
        self.lock = asyncio.Lock()
        self.router = APIRouter()
        route = "/api/documents/{document_id}/pages/{page}/calibration"
        self.router.add_api_route(route, self.get, methods=["GET"])
        self.router.add_api_route(route, self.calibrate, methods=["POST"])

    def _source(self, document_id, page):
        record = self.store.get("source_pages", page_id(document_id, page))
        if not record:
            raise HTTPException(404, "该页未导入。")
        return record

    def get(self, document_id: str, page: int):
        source = self._source(document_id, page)
        for report in reversed(self.store.all("source_calibrations")):
            if (report["document_id"] == document_id and report["page"] == page
                    and (report["base_revision"] == source["revision"]
                         or (report.get("applied_revision") == source["revision"] and source.get("calibration_id") == report["id"]))):
                return report
        return {"status": "not_checked", "page": page, "base_revision": source["revision"]}

    async def calibrate(self, document_id: str, page: int, body: CalibrationRequest):
        # One page at a time bounds paid requests and local OCR work.
        if self.lock.locked():
            raise HTTPException(429, "已有原页校准在进行，请等待完成。")
        async with self.lock:
            original = self._source(document_id, page)
            if original["revision"] != body.revision:
                raise HTTPException(409, "原文版本已变化，请刷新后重新校准。")
            if original["status"] == "reviewed" or machine_verified(original):
                return {"status": "already_verified", "page": page, "base_revision": body.revision}
            if len(original.get("text", "")) > 16000:
                raise HTTPException(422, "此页文字较长，请拆分页或手工校对后继续制卡。")
            settings = self.settings()
            if settings.mode != "cloud" or not all((settings.base_url, settings.model, settings.api_key)):
                raise HTTPException(422, "请先配置支持图片输入的云端模型。")
            configuration = {"model": settings.model, "endpoint_hash": text_hash(settings.base_url), "policy": SOURCE_POLICY_VERSION}
            cached = self.get(document_id, page)
            if not body.force and cached.get("configuration") == configuration:
                return cached
            document = self.store.get("documents", document_id)
            if not document or not document.get("file_url") or not re.fullmatch(r"pdf-[0-9a-f]{24}", document_id):
                raise HTTPException(422, "此材料没有可用的原始 PDF。")
            path = self.storage / "uploads" / f"{document_id}.pdf"
            if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
                raise HTTPException(422, "原始 PDF 不存在或超过处理上限。")
            content = path.read_bytes()
            try:
                image = await asyncio.to_thread(render_page_png, content, page)
                ai = self.ai()
                first = await ai.transcribe_source_page(image, reference_text=original.get("text", ""))
                first_usage = dict(getattr(ai, "last_usage", None) or {})
                second = await ai.transcribe_source_page(image)  # Blind read: no OCR/model text is supplied.
                second_usage = dict(getattr(ai, "last_usage", None) or {})
                result = await asyncio.to_thread(assess, original, first, second)
                supported = False
                if not result["issues"]:
                    try:
                        supported = await asyncio.to_thread(corroborate_changes, content, original, result)
                    except ValueError:
                        result["issues"].append("本地局部复识别未完成，候选修正需人工确认。")
            except (AIError, ValueError):
                # Do not publish an unchecked transcription or treat failure as a pass.
                raise HTTPException(422, "原页校准未完成；请确认模型支持图片输入，或对照原页手动校对。") from None
            if path.read_bytes() != content:
                raise HTTPException(409, "校准期间原始 PDF 已变化，请重新导入并核对。")
            current = self._source(document_id, page)
            if current["revision"] != body.revision:
                raise HTTPException(409, "校准期间原文已被修改，旧结果没有覆盖当前内容。")
            report = {"id": "cal-" + uuid4().hex, "document_id": document_id, "page": page,
                      "base_revision": body.revision, "original_text": original.get("text", ""), **result,
                      "policy_version": SOURCE_POLICY_VERSION, "configuration": configuration,
                      "visual_readings": [first.model_dump(), second.model_dump()], "model_requests": 2,
                      "usage": [first_usage, second_usage],
                      "page_image_sha256": text_hash_bytes(image), "local_changes_supported": supported,
                      "status": "needs_confirmation", "created_at": datetime.now(timezone.utc).isoformat()}
            if supported and not result["issues"]:
                revision = body.revision + 1
                proof = {"policy_version": SOURCE_POLICY_VERSION, "method": "two_visual_reads_and_local_changes",
                         "revision": revision, "parent_revision": body.revision, "text_sha256": text_hash(result["proposed_text"]),
                         "page_image_sha256": report["page_image_sha256"], "visual_reads_agree": True,
                         "protected_content_unchanged": True, "local_changes_supported": True}
                report.update(status="auto_applied", applied_revision=revision)
                save_page_revision(self.store, document_id, page, body.revision,
                    {"text": result["proposed_text"], "status": "auto_verified", "reviewed_at": None,
                     "verification": proof, "calibration_id": report["id"], "calibration_status": "auto_applied",
                     "calibration_issues": []}, "auto_calibrate", extra_records=[("source_calibrations", report)])
            else:
                def commit():
                    current = self._source(document_id, page)
                    if current["revision"] != body.revision:
                        raise HTTPException(409, "原文已更新，请重新校准。")
                    return [("source_calibrations", report), ("source_pages", {**current,
                        "calibration_id": report["id"], "calibration_status": "needs_confirmation",
                        "calibration_issues": report["issues"]})]
                self.store.mutate(commit)
            return report


def text_hash_bytes(value):
    import hashlib
    return hashlib.sha256(value).hexdigest()
