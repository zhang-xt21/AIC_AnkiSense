"""Durable answer analysis shared by the web UI and the Anki integration."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from .evidence import validate_card, validate_diagnosis
from .llm import AIError
from .prompts import DIAGNOSE_PROMPT_VERSION
from .schemas import CardDraft, SourceBlock
from .sources import require_current_reviewed


logger = logging.getLogger(__name__)
ANALYSIS_FAILURE = "分析暂未完成；答案已保存，可以重试。"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _card_data(card):
    return CardDraft.model_validate({name: card[name] for name in CardDraft.model_fields})


class AttemptService:
    def __init__(self, store, settings, ai_getter):
        self.store = store
        self.settings = settings
        # Read on each analysis so changing app.state.ai also affects retries.
        self.ai_getter = ai_getter
        self._locks = {}
        self._tasks = {}
        self._closed = False

    @asynccontextmanager
    async def _lock(self, key):
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                self._locks.pop(key, None)

    def _available(self):
        if self._closed:
            raise HTTPException(503, "服务正在关闭，请稍后重试。")

    def _record(self, collection, identifier):
        record = self.store.get(collection, identifier)
        if record is None:
            raise HTTPException(404, "记录不存在。")
        return record

    def get(self, identifier, expected_origin=None, client_id=None):
        """Return an attempt, optionally restricted to one integration client."""
        attempt = self._record("attempts", identifier)
        if ((expected_origin is not None and attempt.get("origin", "web") != expected_origin)
                or (client_id is not None and attempt.get("client_id") != client_id)):
            # Do not reveal that another client's attempt exists.
            raise HTTPException(404, "记录不存在。")
        return attempt

    def _sources(self, card):
        try:
            identifiers = {entry["block_id"] for entry in card["evidence"]}
            blocks = [SourceBlock.model_validate(self._record("blocks", identifier))
                      for identifier in sorted(identifiers)]
            if any(block.document_id != card["document_id"] for block in blocks):
                raise ValueError("Source document does not match card")
            validate_card(_card_data(card), blocks)
        except (ValueError, KeyError, TypeError):
            # Pydantic errors may include source contents, so use a fixed public
            # message instead of forwarding validation details.
            raise HTTPException(422, "题目的原文引用或评分要点无效，请先重新审核卡片。") from None
        return blocks

    def _current_sources(self, card):
        if card["status"] != "approved":
            raise HTTPException(409, "请先审核并批准这道题。")
        if card.get("source_outdated"):
            raise HTTPException(409, "题目的原文已更新，请更新引用并重新审核卡片。")
        blocks = self._sources(card)
        require_current_reviewed(self.store, blocks)
        return blocks

    @staticmethod
    def _check_replay(attempt, card_id, answer, expected_version, origin, client_id):
        if (attempt["card_id"] != card_id or attempt["answer"] != answer
                or attempt.get("expected_version") != expected_version
                or attempt.get("origin", "web") != origin
                or attempt.get("client_id") != client_id):
            raise HTTPException(409, "该提交编号已用于不同的回答、版本或客户端。")

    async def submit(self, card_id, answer, request_id, expected_version=None,
                     origin="web", client_id=None):
        self._available()
        if origin not in {"web", "anki"}:
            raise HTTPException(422, "不支持的答题来源。")
        if origin == "anki" and (not client_id or type(expected_version) is not int
                                 or expected_version < 1):
            raise HTTPException(422, "插件答题必须提供客户端编号和卡片版本。")
        if expected_version is not None and (type(expected_version) is not int
                                             or expected_version < 1):
            raise HTTPException(422, "卡片版本必须为正整数。")
        async with self._lock("request:" + request_id):
            self._available()
            attempt = None
            created = False

            def build():
                nonlocal attempt, created
                # Checking under the SQLite writer lock also protects two
                # service instances from racing the unique request index.
                attempt = self.store.find_request(request_id)
                if attempt is not None:
                    self._check_replay(attempt, card_id, answer, expected_version,
                                       origin, client_id)
                    return []
                card = self._record("cards", card_id)
                if expected_version is not None and card["version"] != expected_version:
                    raise HTTPException(409, "题目版本已更新，请同步后重新作答。")
                blocks = self._current_sources(card)
                attempt = {
                    "id": str(uuid4()), "request_id": request_id,
                    "card_id": card_id, "answer": answer, "status": "pending",
                    "diagnosis": None, "error": None, "remediation": None,
                    "card_snapshot": card,
                    "source_snapshot": [block.model_dump() for block in blocks],
                    "is_demo": self.settings.mode == "demo",
                    "model": self._model(), "prompt_version": DIAGNOSE_PROMPT_VERSION,
                    "created_at": _now(), "retry_count": 0,
                    "origin": origin, "expected_version": expected_version,
                }
                if client_id is not None:
                    attempt["client_id"] = client_id
                created = True
                return [("attempts", attempt)]

            self.store.mutate(build)
            if created:
                task = self._start_analysis(attempt)
            else:
                task = self._tasks.get(attempt["id"])
            if task is not None:
                # A network disconnect must not discard an already saved answer
                # or lead a reconnecting client to pay for the same call twice.
                return await asyncio.shield(task)
            return attempt

    def _model(self):
        return self.settings.model if self.settings.mode == "cloud" else "demo-rules-v1"

    def _start_analysis(self, attempt):
        task = asyncio.create_task(self._analyze(attempt))
        self._tasks[attempt["id"]] = task

        def finished(completed):
            if self._tasks.get(attempt["id"]) is completed:
                self._tasks.pop(attempt["id"], None)
            # Retrieve exceptions even when the original HTTP caller left.
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        return task

    async def retry(self, identifier, expected_origin=None, client_id=None, expected_retry_count=None):
        self._available()
        if expected_retry_count is not None and (type(expected_retry_count) is not int
                                                  or not 0 <= expected_retry_count <= 2):
            raise HTTPException(422, "重试次数必须为 0 到 2 之间的整数。")
        async with self._lock("attempt:" + identifier):
            self._available()
            attempt = None

            def build():
                nonlocal attempt
                attempt = self.get(identifier, expected_origin, client_id)
                if expected_retry_count is not None and attempt["retry_count"] != expected_retry_count:
                    raise HTTPException(409, "该回答的重试状态已变化，请先刷新分析结果。")
                if attempt["status"] != "failed":
                    raise HTTPException(409, "仅分析失败的回答可以重试。")
                if attempt["retry_count"] >= 2:
                    raise HTTPException(409, "已达到本次回答的重试上限，请检查模型配置后重新作答。")
                attempt.setdefault("analysis_history", []).append({
                    key: attempt.get(key) for key in (
                        "status", "error", "model", "is_demo", "prompt_version",
                        "elapsed_seconds", "finished_at", "usage")
                })
                attempt.update(status="pending", error=None,
                               retry_count=attempt["retry_count"] + 1)
                return [("attempts", attempt)]

            # Atomic failed -> pending prevents other service instances from
            # launching another retry of this same failure.
            self.store.mutate(build)
            return await asyncio.shield(self._start_analysis(attempt))

    async def _analyze(self, attempt):
        started = time.perf_counter()
        attempt.update(is_demo=self.settings.mode == "demo", model=self._model(),
                       prompt_version=DIAGNOSE_PROMPT_VERSION, usage=None)
        self.store.put("attempts", attempt)
        try:
            if not attempt["answer"].strip():
                attempt.update(status="unanswered", error=None)
                return attempt
            card = attempt["card_snapshot"]
            blocks = [SourceBlock.model_validate(block) for block in attempt["source_snapshot"]]
            ai = self.ai_getter()
            diagnosis = await ai.diagnose_answer(_card_data(card), blocks, attempt["answer"])
            usage = getattr(ai, "last_usage", None)
            validate_diagnosis(diagnosis, _card_data(card), blocks, attempt["answer"])
            remediation = None
            targets = set(diagnosis.missing_point_ids + diagnosis.contradicted_point_ids)
            used = {entry["card_id"] for entry in self.store.all("attempts")}
            if targets:
                for candidate in self.store.all("cards"):
                    if (candidate["status"] == "approved" and candidate["kind"] == "remediation"
                            and candidate["concept"] == card["concept"]
                            and candidate["document_id"] == card["document_id"]
                            and candidate["id"] not in used
                            and targets.intersection(point["point_id"] for point in candidate["rubric"])):
                        try:
                            self._current_sources(candidate)
                        except HTTPException:
                            # An obsolete or invalid follow-up must not make
                            # the student's otherwise valid diagnosis fail.
                            continue
                        remediation = candidate
                        break
            attempt.update(status="completed", diagnosis=diagnosis.model_dump(),
                           remediation=remediation, error=None, usage=usage)
        except asyncio.CancelledError:
            attempt.update(status="failed", error="分析被服务关闭中断；答案已保存，可以重试。",
                           diagnosis=None, remediation=None)
            raise
        except AIError as error:
            # AIError is explicitly the adapter's sanitized public error type.
            attempt.update(status="failed", error=str(error), diagnosis=None, remediation=None)
        except Exception:
            # Validation errors can contain raw provider output. Never return
            # their text or include submitted data and credentials in logs.
            logger.error("Analysis failed unexpectedly for attempt %s", attempt["id"])
            attempt.update(status="failed", error=ANALYSIS_FAILURE, diagnosis=None, remediation=None)
        finally:
            attempt["finished_at"] = _now()
            attempt["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            self.store.put("attempts", attempt)
        return attempt

    async def shutdown(self):
        self._closed = True
        identifiers = list(self._tasks)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # A just-created task may have been cancelled before _analyze started.
        for identifier in identifiers:
            attempt = self.store.get("attempts", identifier)
            if attempt and attempt["status"] == "pending":
                attempt.update(status="failed", error="分析被服务关闭中断；答案已保存，可以重试。",
                               finished_at=_now())
                self.store.put("attempts", attempt)
        self._tasks.clear()
