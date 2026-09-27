"""Small server-side adapter for explicit demos and compatible cloud APIs."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from pydantic import ValidationError

from .demo import seed_records
from .evidence import validate_card, validate_diagnosis
from .prompts import COMMON_INSTRUCTIONS, DIAGNOSE_INSTRUCTIONS, GENERATE_INSTRUCTIONS
from .schemas import CardDraft, Diagnosis, DraftBatch, SourceBlock


class AIError(RuntimeError):
    """A safe, user-visible error that never includes request data or credentials."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Settings:
    mode: str = "demo"
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 45.0

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
        try:
            timeout = float(os.getenv("AI_TIMEOUT_SECONDS", "45"))
        except ValueError:
            raise AIError("AI_TIMEOUT_SECONDS 必须是 1 到 180 之间的秒数。") from None
        if not 1 <= timeout <= 180:
            raise AIError("AI_TIMEOUT_SECONDS 必须是 1 到 180 之间的秒数。")
        mode = os.getenv("AI_MODE", "demo").strip().lower()
        if mode not in {"demo", "cloud"}:
            raise AIError("AI_MODE 只能设置为 demo 或 cloud。")
        return cls(
            mode=mode,
            base_url=os.getenv("AI_BASE_URL", "").strip().rstrip("/"),
            model=os.getenv("AI_MODEL", "").strip(),
            api_key=os.getenv("AI_API_KEY", "").strip(),
            timeout_seconds=timeout,
        )


class AIService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_usage: dict[str, int] | None = None

    async def generate_drafts(self, blocks: list[SourceBlock], count: int) -> DraftBatch:
        if self.settings.mode == "demo":
            raise AIError("演示模式仅支持内置示例卡片；生成新卡片需要配置云端模型 API。")
        if not blocks or not 1 <= count <= 6:
            raise AIError("请选取讲义片段，并指定 1 到 6 张卡片。")
        result = await self._request(
            GENERATE_INSTRUCTIONS,
            {"count": count, "source_blocks": [block.model_dump() for block in blocks]},
            DraftBatch.model_json_schema(),
        )
        try:
            batch = DraftBatch.model_validate(result)
            if len(batch.cards) != count:
                raise ValueError("生成数量不符。")
            for card in batch.cards:
                validate_card(card, blocks)
        except (ValidationError, ValueError):
            raise AIError("模型返回的卡片格式或原文引用未通过校验，请重新生成。") from None
        return batch

    async def diagnose_answer(
        self, card: CardDraft, blocks: list[SourceBlock], answer: str
    ) -> Diagnosis:
        try:
            validate_card(card, blocks)
        except ValueError:
            raise AIError("题目的原文引用或评分要点无效，请先重新审核卡片。") from None
        if not answer.strip():
            raise AIError("请先填写答案。")
        if self.settings.mode == "demo":
            diagnosis = self._demo_diagnosis(card, blocks, answer)
        else:
            cited_ids = {evidence.block_id for evidence in card.evidence}
            result = await self._request(
                DIAGNOSE_INSTRUCTIONS,
                {
                    "card": card.model_dump(),
                    "source_blocks": [
                        block.model_dump() for block in blocks if block.block_id in cited_ids
                    ],
                    "student_answer": answer,
                },
                Diagnosis.model_json_schema(),
            )
            try:
                diagnosis = Diagnosis.model_validate(result)
            except (ValidationError, ValueError):
                raise AIError("模型返回的分析格式不正确，请重新分析。") from None
        try:
            validate_diagnosis(diagnosis, card, blocks, answer)
        except ValueError:
            raise AIError("模型分析的要点或引用未通过校验，请重新分析。") from None
        return diagnosis

    async def _request(self, instruction: str, data: dict, schema: dict) -> dict:
        self.last_usage = None
        settings = self.settings
        if settings.mode != "cloud":
            raise AIError("AI_MODE 只能设置为 demo 或 cloud。")
        if not settings.api_key or not settings.base_url or not settings.model:
            raise AIError("云端模式需要配置 AI_API_KEY、AI_BASE_URL 和 AI_MODEL。")
        try:
            endpoint = urlsplit(settings.base_url)
        except ValueError:
            raise AIError("模型接口地址格式不正确。") from None
        secure = endpoint.scheme == "https"
        local = endpoint.scheme == "http" and endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not (secure or local)
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise AIError("云端接口必须使用不含账号、查询参数或片段的 HTTPS 地址。")
        payload = {
            "model": settings.model,
            "messages": [
                {
                    "role": "system",
                    "content": COMMON_INSTRUCTIONS + json.dumps(schema, ensure_ascii=False) + "\n" + instruction,
                },
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "max_tokens": 4096,
        }
        try:
            async with httpx.AsyncClient(timeout=settings.timeout_seconds, follow_redirects=False) as client:
                response = await client.post(
                    settings.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + settings.api_key},
                    json=payload,
                )
                response.raise_for_status()
        except httpx.TimeoutException:
            raise AIError("模型调用超时，请稍后重试。") from None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                message = "模型服务拒绝访问，请检查 API Key 和模型权限。"
            elif status == 429:
                message = "模型服务额度不足或请求过于频繁，请检查额度后重试。"
            else:
                message = "模型服务暂时不可用，请检查模型配置或稍后重试。"
            raise AIError(message) from None
        except (httpx.RequestError, httpx.InvalidURL):
            raise AIError("无法连接模型服务，请检查网络与接口地址后重试。") from None
        try:
            body = response.json()
            choice = body["choices"][0]
            if choice.get("finish_reason") not in {None, "stop"}:
                raise ValueError("输出未正常完成。")
            content = choice["message"]["content"]
            if not isinstance(content, str) or len(content) > 100000:
                raise ValueError("输出格式或长度不正确。")
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("输出必须是对象。")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise AIError("模型未返回完整的 JSON 结果，请重试或更换支持 JSON 输出的模型。") from None
        usage = body.get("usage")
        if isinstance(usage, dict):
            self.last_usage = {
                key: value
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                if isinstance(value := usage.get(key), int) and not isinstance(value, bool) and value >= 0
            }
        return result

    def _demo_diagnosis(
        self, card: CardDraft, blocks: list[SourceBlock], answer: str
    ) -> Diagnosis:
        if not blocks or any(block.document_id != "demo-stacks-queues" for block in blocks):
            raise AIError("演示规则仅适用于内置《栈与队列》示例；自有讲义需要配置云端模型。")
        source_ids = list(dict.fromkeys(evidence.block_id for evidence in card.evidence))
        result = {
            "verdict": "uncertain",
            "missing_point_ids": [],
            "contradicted_point_ids": [],
            "answer_spans": [],
            "source_block_ids": source_ids,
            "reason": "演示规则：当前表达不在内置规则覆盖范围，无法可靠判断。请启用云端模型进行真实分析。",
        }
        records = seed_records()
        canonical = next(
            (
                {name: record[name] for name in CardDraft.model_fields}
                for table, record in records
                if table == "cards" and record["kind"] == "basic" and record["question"] == card.question
            ),
            None,
        )
        expected_blocks = {record["block_id"]: record for table, record in records if table == "blocks"}
        if canonical != card.model_dump() or any(
            block.model_dump() != expected_blocks.get(block.block_id) for block in blocks
        ):
            result["reason"] = "演示规则：仅支持未经修改的内置示例题与讲义；当前内容需要云端模型分析。"
            return Diagnosis.model_validate(result)
        normalized = re.sub(r"[\s，。！？、；：,.!?;:]", "", answer).lower()
        point_ids = {point.point_id for point in card.rubric}
        # Full-string matching is deliberate: a keyword elsewhere in an answer
        # must never turn an arbitrary, negated, or contradictory response correct.
        rules = {
            "栈的出入顺序规则是什么？": (
                "stack_lifo", {"后进先出", "先进后出", "lifo", "lastinfirstout", "最后入栈的元素最先出栈", "栈遵循后进先出最后入栈的元素最先出栈"},
                {"先进先出", "后进后出", "fifo", "firstinfirstout"},
                "栈遵循后进先出（LIFO）。",
            ),
            "队列的出入顺序规则是什么？": (
                "queue_fifo", {"先进先出", "后进后出", "fifo", "firstinfirstout", "最先入队的元素最先出队", "队列遵循先进先出最先入队的元素最先出队"},
                {"后进先出", "先进后出", "lifo", "lastinfirstout"},
                "队列遵循先进先出（FIFO）。",
            ),
        }
        rule = rules.get(card.question)
        if rule and point_ids == {rule[0]}:
            point_id, correct_answers, incorrect_answers, explanation = rule
            if normalized in correct_answers | incorrect_answers:
                correct = normalized in correct_answers
                result.update(
                    verdict="correct" if correct else "incorrect",
                    contradicted_point_ids=[] if correct else [point_id],
                    answer_spans=[answer.strip()],
                    reason="演示规则：" + explanation + "本次仅匹配内置表达，未调用 AI 模型。",
                )
        elif card.question == "空栈能直接执行出栈吗？为什么？" and point_ids == {"stack_empty", "stack_check"}:
            if normalized in {"不能", "不可以", "不能出栈", "空栈不能出栈"}:
                result.update(
                    verdict="incomplete", missing_point_ids=["stack_empty", "stack_check"],
                    addressed_point_ids=["stack_empty"],
                    answer_spans=[answer.strip()],
                    reason="演示规则：已说明不能出栈，还需说明空栈没有可弹出的元素、直接出栈会下溢，以及出栈前应先判空。未调用 AI 模型。",
                )
            elif normalized in {"可以", "可以直接出栈", "能直接出栈"}:
                result.update(
                    verdict="incorrect", contradicted_point_ids=["stack_empty"],
                    answer_spans=[answer.strip()],
                    reason="演示规则：空栈不能直接出栈，否则会发生下溢。未调用 AI 模型。",
                )
            elif normalized in {"不能出栈前要先判空避免下溢", "不能出栈前应检查是否为空避免下溢"}:
                result.update(
                    verdict="incomplete", missing_point_ids=["stack_empty"],
                    addressed_point_ids=["stack_check"],
                    answer_spans=[answer.strip()],
                    reason="演示规则：已说明出栈前判空，还需说明空栈没有可弹出的元素。未调用 AI 模型。",
                )
            elif normalized in {
                "不能空栈没有可弹出的元素出栈前应判空避免下溢",
                "不能空栈没有可弹出的元素直接出栈会下溢出栈前应先判空",
            }:
                result.update(
                    verdict="correct", answer_spans=[answer.strip()],
                    reason="演示规则：回答覆盖空栈不能出栈及出栈前判空两个要点。未调用 AI 模型。",
                )
        return Diagnosis.model_validate(result)
