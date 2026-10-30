"""Card-scoped visual checks; these never claim a page was human reviewed."""
import hashlib
import json

from .schemas import CardDraft

POLICY = "automatic-card-visual-v5-target-scope"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def content_hash(card):
    return fingerprint({**{key: card.get(key) for key in CardDraft.model_fields}, "images": card.get("images", [])})


def block_hash(blocks):
    return fingerprint([b.model_dump() for b in sorted({b.block_id: b for b in blocks}.values(), key=lambda b: b.block_id)])


def automatic_evidence_valid(card, blocks):
    if not card or card.get("approval_method") != "automatic":
        return False
    quality = card.get("auto_quality") or {}
    proof = card.get("automatic_evidence") or {}
    from .reference_check import reference_answer_passes, minimal_answer_passes
    try:
        draft = CardDraft.model_validate({key: card[key] for key in CardDraft.model_fields})
    except (ValueError, KeyError, TypeError):
        return False
    return (card.get("status") == "approved" and not card.get("source_outdated")
            and quality.get("verdict") == "clear" and not quality.get("issues")
            and quality.get("scope") == "text_and_original_page"
            and quality.get("card_version") == card.get("version")
            and reference_answer_passes(quality.get("reference_answer_check"), draft, blocks)
            and minimal_answer_passes(quality.get("minimal_answer_check"), draft, blocks)
            and proof.get("policy") == POLICY and proof.get("visual_check") is True
            and proof.get("card_hash") == content_hash(card)
            and proof.get("block_hash") == block_hash(blocks)
            and bool(proof.get("page_images"))
            and all(isinstance(v, str) and len(v) == 64 for v in proof["page_images"].values())
            and {str(b.page) for b in blocks}.issubset(proof["page_images"]))
