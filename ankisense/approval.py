"""Validate saved approval receipts without invoking a model or changing sources."""

import hashlib
import json

from fastapi import HTTPException

from .schemas import CardDraft, CardImage, SourceBlock
from .sources import page_id, require_current_reviewed, require_current_source, source_available
from .automatic_policy import automatic_evidence_valid
from .source_policy import source_usable
from .evidence import validate_card
from .prompts import CARD_REVIEW_PROMPT_VERSION


MACHINE_APPROVAL_POLICY = "whole-card-review-v7-target-scope"


def card_binding_hash(card):
    """Bind approval to content, evidence, attachments and stable identity."""
    draft = CardDraft.model_validate({key: card[key] for key in CardDraft.model_fields if key in card})
    binding = {key: card[key] for key in ("id", "version", "document_id")}
    binding.update(draft.model_dump())
    binding["images"] = [CardImage.model_validate(item).model_dump() for item in card.get("images") or []]
    intent = card.get("image_intent")
    # Keep old text-card receipts valid when the optional intent is absent or
    # explicitly "none". A meaningful image requirement is approval content.
    if intent and (not isinstance(intent, dict) or intent.get("need") != "none"):
        binding["image_intent"] = intent
    return hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def require_required_image(store, card):
    """A picture-dependent question needs a current, cited front-side image.

    This checks local prerequisites only. Whole-card machine approval still
    requires the separate visual review of every attached image.
    """
    intent = card.get("image_intent")
    if not intent:
        return  # Existing cards predate automatic image intent.
    if not isinstance(intent, dict) or intent.get("need") not in {"none", "helpful", "required"}:
        raise HTTPException(409, "卡片的配图要求无效，请重新检查卡片。")
    if intent["need"] != "required":
        return

    identifiers = intent.get("block_ids")
    cited_ids = {item.get("block_id") for item in card.get("evidence") or [] if isinstance(item, dict)}
    if (not isinstance(identifiers, list) or not identifiers
            or any(not isinstance(value, str) or value not in cited_ids for value in identifiers)):
        raise HTTPException(409, "这道题需要看图作答，但配图要求未关联当前引用，请先更新卡片。")

    pages = set()
    for identifier in identifiers:
        block = store.get("blocks", identifier)
        if not block or block.get("document_id") != card.get("document_id"):
            raise HTTPException(409, "这道题所需图片的引用片段不可用，请先更新引用。")
        current = store.get("source_pages", page_id(card["document_id"], block.get("page")))
        if (not source_available(current)
                or identifier not in current.get("block_ids", []) or not current.get("text", "").strip()):
            raise HTTPException(409, "这道题所需图片的引用页已更新或不可用，请先更新引用。")
        pages.add((block["page"], current.get("revision")))

    for attachment in card.get("images") or []:
        if not isinstance(attachment, dict) or attachment.get("side") != "question":
            continue
        media = store.get("media", attachment.get("media_id", "")) or {}
        source = media.get("source") or {}
        if (isinstance(source, dict) and source.get("document_id") == card.get("document_id")
                and (source.get("page"), source.get("revision")) in pages):
            return
    raise HTTPException(409, "这道题需要看图作答，请先在题目侧添加来自所选引用页、当前版本的讲义图片。")


def valid_machine_approval(store, card):
    """Only a successful server review of the current content can approve it.

    Source confirmation metadata is deliberately excluded: confirming an
    unchanged extraction must not invalidate an existing approval.
    """
    try:
        require_required_image(store, card)
        receipt = card.get("approval") or {}
        if (card.get("status") != "approved" or card.get("source_outdated")
                or receipt.get("method") != "machine"
                or receipt.get("card_version") != card.get("version")):
            return False
        record = store.get("card_auto_reviews", receipt.get("review_id", ""))
        if (not record or record.get("review_id") != receipt.get("review_id")
                or record.get("card_id") != card.get("id")
                or record.get("card_version") != card.get("version")
                or record.get("policy_version") != MACHINE_APPROVAL_POLICY
                or record.get("prompt_version") != CARD_REVIEW_PROMPT_VERSION
                or record.get("status") != "auto_pass" or record.get("retryable") is not False
                or record.get("card_binding_hash") != card_binding_hash(card)):
            return False
        document = store.get("documents", card["document_id"]) or {}
        if document.get("pdf_sha256") and record.get("pdf_hash") != document["pdf_sha256"]:
            return False

        source_blocks = record.get("source_blocks")
        source_pages = record.get("source_pages")
        if not isinstance(source_blocks, list) or not source_blocks or not isinstance(source_pages, dict):
            return False
        block_ids = {entry["block_id"] for entry in card["evidence"]}
        if {entry["block_id"] for entry in source_blocks} != block_ids:
            return False
        blocks = []
        for captured in source_blocks:
            current = store.get("blocks", captured["block_id"])
            if current != captured or captured["document_id"] != card["document_id"]:
                return False
            blocks.append(SourceBlock.model_validate(current))
        require_current_source(store, blocks)
        from .reference_check import reference_answer_passes, minimal_answer_passes
        draft = CardDraft.model_validate({key: card[key] for key in CardDraft.model_fields})
        if not reference_answer_passes(record.get("reference_answer_check"), draft, blocks):
            return False
        if not minimal_answer_passes(record.get("minimal_answer_check"), draft, blocks):
            return False
        if set(source_pages) != {str(block.page) for block in blocks}:
            return False
        for number, captured in source_pages.items():
            current = store.get("source_pages", page_id(card["document_id"], int(number)))
            if (not source_available(current) or not captured
                    or not current.get("text", "").strip()
                    or any(current.get(key) != captured.get(key) for key in ("revision", "block_ids", "text"))):
                return False

        attachments = card.get("images") or []
        bindings = record.get("image_bindings")
        if not isinstance(bindings, list) or len(bindings) != len(attachments):
            return False
        from .image_auto_review import POLICY_VERSION as IMAGE_POLICY_VERSION
        from .figure_vision import PROMPT_VERSION as IMAGE_PROMPT_VERSION
        for attachment, binding in zip(attachments, bindings):
            image = store.get("image_auto_reviews", binding.get("review_id", ""))
            media = store.get("media", attachment["media_id"])
            source = (media or {}).get("source")
            if (binding.get("attachment") != attachment or binding.get("current_pass") is not True
                    or binding.get("media_id") != attachment["media_id"]
                    or not image or image.get("card_id") != card["id"]
                    or image.get("card_version") != card["version"]
                    or image.get("media_id") != attachment["media_id"]
                    or image.get("policy_version") != IMAGE_POLICY_VERSION
                    or image.get("prompt_version") != IMAGE_PROMPT_VERSION
                    or image.get("fingerprint") != binding.get("image_fingerprint")
                    or image.get("status") != "auto_pass" or image.get("retryable") is not False
                    or (media.get("sha256") and image.get("image_hash") != media["sha256"])
                    or not source or source != binding.get("media_source")
                    or image.get("source_revision") != source.get("revision")
                    or image.get("bbox") != source.get("bbox")
                    or image.get("side") != attachment.get("side", "answer")):
                return False
            require_image_source(store, card, media)
        return True
    except (HTTPException, KeyError, TypeError, ValueError, AttributeError):
        return False


def _card_blocks(store, card):
    blocks = []
    for identifier in dict.fromkeys(entry["block_id"] for entry in card["evidence"]):
        block = store.get("blocks", identifier)
        if not block or block.get("document_id") != card.get("document_id"):
            raise HTTPException(409, "卡片引用的原文缺失或来自其他材料，请重新检查卡片。")
        blocks.append(SourceBlock.model_validate(block))
    return blocks


def valid_automatic_approval(store, card, blocks=None):
    """Validate LIN's original proof without manufacturing a new review receipt."""
    try:
        blocks = _card_blocks(store, card) if blocks is None else blocks
        if not automatic_evidence_valid(card, blocks):
            return False
        require_current_source(store, blocks)
        validate_card(CardDraft.model_validate({key: card[key] for key in CardDraft.model_fields if key in card}), blocks)
        require_required_image(store, card)
        for attachment in card.get("images") or []:
            media = store.get("media", attachment["media_id"])
            if not media:
                return False
            source = media.get("source") or {}
            if source:
                current = store.get("source_pages", page_id(card["document_id"], source.get("page")))
                if (source.get("document_id") != card["document_id"] or not source_available(current)
                        or current.get("revision") != source.get("revision") or not current.get("text", "").strip()):
                    return False
                if (source.get("origin") == "automatic_source_page"
                        and str(source.get("page")) not in card["automatic_evidence"]["page_images"]):
                    return False
        return True
    except (HTTPException, KeyError, TypeError, ValueError, AttributeError):
        return False


def require_approved_sources(store, card, blocks):
    """Shared gate for practice, export and sync; no per-page human prerequisite."""
    if card.get("status") != "approved" or card.get("source_outdated"):
        raise HTTPException(409, "卡片尚未通过审核或原文已更新，请重新检查卡片。")
    try:
        current_blocks = _card_blocks(store, card)
        if {block.block_id: block for block in current_blocks} != {block.block_id: block for block in blocks}:
            raise ValueError("source snapshot mismatch")
        validate_card(CardDraft.model_validate({key: card[key] for key in CardDraft.model_fields if key in card}), blocks)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise HTTPException(409, "卡片内容或原文引用已失效，请重新检查卡片。") from error
    require_required_image(store, card)
    receipt = card.get("approval") or {}
    method = receipt.get("method") if isinstance(receipt, dict) else None
    if method == "machine":
        if not valid_machine_approval(store, card):
            raise HTTPException(409, "机器审核记录已失效，请重新检查卡片。")
        require_current_source(store, blocks)
    elif method == "human":
        if receipt.get("card_version") != card.get("version"):
            raise HTTPException(409, "人工审核对应的卡片版本已变化，请重新检查卡片。")
        require_current_source(store, blocks)
    elif receipt:
        raise HTTPException(409, "卡片审核记录无效，请重新检查卡片。")
    elif card.get("approval_method") == "automatic":
        if not valid_automatic_approval(store, card, blocks):
            raise HTTPException(409, "自动制卡的原始检查证明已失效，请重新检查卡片。")
    else:
        require_current_reviewed(store, blocks)
    for attachment in card.get("images") or []:
        try:
            media = store.get("media", attachment["media_id"])
            if not media:
                raise HTTPException(409, "卡片配图记录缺失，请重新添加图片。")
            require_image_source(store, card, media)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise HTTPException(409, "卡片配图信息无效，请重新检查配图。") from error


def display_card(store, card):
    """Present obsolete machine receipts as pending without rewriting history."""
    receipt = (card or {}).get("approval") or {}
    machine_stale = isinstance(receipt, dict) and receipt.get("method") == "machine" and not valid_machine_approval(store, card)
    automatic_stale = (card and not receipt and card.get("approval_method") == "automatic"
                       and not valid_automatic_approval(store, card))
    if card and card.get("status") == "approved" and (machine_stale or automatic_stale):
        return {**card, "status": "draft", "approval_stale": True,
                "review_state": card.get("review_state") if card.get("review_state") in {"queued", "running"} else "needs_review"}
    return card


def require_image_source(store, card, media):
    """Check a PDF attachment's current source, retaining legacy approval rules."""
    source = media.get("source") or {}
    if not source:
        return
    if not isinstance(source, dict) or source.get("document_id") != card["document_id"]:
        raise HTTPException(409, "卡片配图与讲义不一致，请重新检查配图。")
    receipt = card.get("approval") or {}
    method = receipt.get("method") if isinstance(receipt, dict) else None
    current = store.get("source_pages", page_id(card["document_id"], source.get("page")))
    automatic = not receipt and card.get("approval_method") == "automatic"
    usable = (source_available(current) if method in {"machine", "human"} or automatic else
              bool(current and source_usable(current)))
    if (not usable or current.get("revision") != source.get("revision")
            or ((method in {"machine", "human"} or automatic) and not current.get("text", "").strip())):
        raise HTTPException(409, "配图所用的原文页已更新或不可用，请重新检查配图。")
    if automatic:
        if not valid_automatic_approval(store, card):
            raise HTTPException(409, "自动配图对应的制卡证明已失效，请重新检查卡片。")
        if source.get("origin") == "automatic_source_page":
            pages = (card.get("automatic_evidence") or {}).get("page_images") or {}
            if str(source.get("page")) not in pages:
                raise HTTPException(409, "自动配图不属于本次检查的原文页，请重新检查配图。")
