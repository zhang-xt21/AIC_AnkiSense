"""A source-bound consistency gate, separate from semantic quality evidence."""
from .evidence import validate_diagnosis
from .schemas import Diagnosis


def reference_answer_passes(value, card, blocks, answer=None):
    try:
        diagnosis = value if isinstance(value, Diagnosis) else Diagnosis.model_validate(value)
        validate_diagnosis(diagnosis, card, list(blocks), card.answer if answer is None else answer)
        return (diagnosis.verdict == "correct"
                and set(diagnosis.addressed_point_ids) == {p.point_id for p in card.rubric})
    except (ValueError, TypeError):
        return False


def minimal_answer_passes(value, card, blocks):
    from .learning_policy import non_learning_reason, question_context_reason, unasked_expression_requirement, multiple_target_reason
    if (non_learning_reason(card.concept, card.question)
            or question_context_reason(card) or multiple_target_reason(card) or unasked_expression_requirement(card)):
        return False
    if not isinstance(value, dict) or not isinstance(value.get("answer"), str) or not value["answer"].strip():
        return False
    return reference_answer_passes(value.get("diagnosis"), card, blocks, value["answer"])
