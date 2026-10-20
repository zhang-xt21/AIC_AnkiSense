"""Machine source verification is explicitly distinct from human review."""
import hashlib
import re

SOURCE_POLICY_VERSION = "source-visual-consensus-v1"


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def machine_verified(page):
    proof = page.get("verification") or {}
    if not isinstance(proof, dict):
        return False
    return (page.get("status") == "auto_verified" and bool(page.get("text", "").strip())
            and proof.get("policy_version") == SOURCE_POLICY_VERSION
            and proof.get("method") == "two_visual_reads_and_local_changes"
            and proof.get("revision") == page.get("revision")
            and proof.get("text_sha256") == text_hash(page["text"])
            and isinstance(proof.get("page_image_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", proof["page_image_sha256"]) is not None
            and proof.get("visual_reads_agree") is True
            and proof.get("protected_content_unchanged") is True
            and proof.get("local_changes_supported") is True)


def source_usable(page):
    return page.get("status") == "reviewed" or machine_verified(page)
