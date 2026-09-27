"""Isolated, explicit cloud enrichment through Docling's picture-description stage.

Exactly one HTTP attempt. No model download, key files or raw provider logging.
Input arrives on stdin; stdout is a small sanitized JSON result.
"""
import base64
import io
import json
import logging
import sys
from urllib.parse import urlsplit

from PIL import Image

MAX_OUTPUT_TOKENS = 4096
FINISH_REASONS = {"stop", "length", "content_filter", "tool_calls", "insufficient_system_resource", "aborted"}


def generation_params(endpoint, model):
    params = {"model": model, "temperature": .1, "max_tokens": MAX_OUTPUT_TOKENS,
              "response_format": {"type": "json_object"}}
    url = urlsplit(endpoint)
    # Official docs (2026-09-27) specify thinking defaults to enabled. This
    # compact schema task deliberately uses non-thinking mode on these two
    # documented models. Do not send vendor extensions to proxies/providers.
    if (url.scheme == "https" and url.hostname == "api.deepseek.com"
            and url.port in {None, 443} and model in {"deepseek-flash", "deepseek-v4-pro"}):
        params["thinking"] = {"type": "disabled"}
    return params


def safe_usage(value):
    if not isinstance(value, dict):
        return None
    result = {key: number for key, number in value.items()
              if key in {"prompt_tokens", "completion_tokens", "total_tokens"} and type(number) is int and 0 <= number <= 100_000_000}
    details = value.get("completion_tokens_details")
    if isinstance(details, dict) and type(details.get("reasoning_tokens")) is int and 0 <= details["reasoning_tokens"] <= 100_000_000:
        result["reasoning_tokens"] = details["reasoning_tokens"]
    return result


def enrich(payload):
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.base_models import ItemAndImageEnrichmentElement
    from docling.datamodel.pipeline_options import PictureDescriptionApiOptions
    from docling.models.stages.picture_description.picture_description_api_model import PictureDescriptionApiModel
    import docling.utils.api_image_request as transport
    from docling_core.types.doc import DoclingDocument
    import requests

    params = generation_params(payload["endpoint"], payload["model"])
    diagnostics = {"max_tokens": MAX_OUTPUT_TOKENS, "thinking": "disabled" if "thinking" in params else "provider_default", "attempts": 0}

    def failure(code):
        return {"error": code, "diagnostics": {**diagnostics, "error_code": code}}

    class OneAttemptSession(requests.Session):
        def post(self, *args, **kwargs):
            kwargs["allow_redirects"] = False
            diagnostics["attempts"] += 1
            try:
                response = super().post(*args, **kwargs)
            except requests.Timeout:
                diagnostics["error_code"] = "timeout"
                raise
            except requests.RequestException:
                diagnostics["error_code"] = "network_error"
                raise
            diagnostics["http_status"] = response.status_code
            if not 200 <= response.status_code < 300:
                diagnostics["error_code"] = "provider_http_error"
                return response
            try:
                body = response.json()
                choices = body.get("choices") if isinstance(body, dict) else None
                choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
                finish = choice.get("finish_reason")
                diagnostics["finish_reason"] = finish if isinstance(finish, str) and finish in FINISH_REASONS else "unknown"
                message = choice.get("message")
                if isinstance(message, dict):
                    diagnostics["content_chars"] = len(message.get("content")) if isinstance(message.get("content"), str) else 0
                    diagnostics["reasoning_chars"] = len(message.get("reasoning_content")) if isinstance(message.get("reasoning_content"), str) else 0
                diagnostics["usage"] = safe_usage(body.get("usage")) if isinstance(body, dict) else None
            except (ValueError, TypeError, AttributeError):
                diagnostics["error_code"] = "provider_invalid_json"
            return response

    # The pinned upstream helper retries POST 5 times by default; a paid check
    # must have a single attempt, and credentials must not follow redirects.
    transport._make_retry_session = OneAttemptSession
    crop_bytes = base64.b64decode(payload["crop"], validate=True)
    base64.b64decode(payload["page"], validate=True)
    with Image.open(io.BytesIO(crop_bytes)) as source:
        image = source.convert("RGB")
    # The pinned Docling API backend forwards params after its default
    # messages. Supply two original PNGs to keep crop detail and make source
    # context explicit, instead of shrinking both into a comparison montage.
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "A CARD CROP — actual final picture placed on the card; evaluate this image."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + payload["crop"]}},
        {"type": "text", "text": "B SOURCE PAGE — original lecture page, reference only; do not treat its contents as visible in A."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + payload["page"]}},
        {"type": "text", "text": payload["prompt"]}]}]
    options = PictureDescriptionApiOptions(url=payload["endpoint"], headers={"Authorization": "Bearer " + payload["api_key"]},
        params={**params, "messages": messages},
        prompt=payload["prompt"], timeout=payload["timeout"], concurrency=1, batch_size=1, picture_area_threshold=0,
        provenance="ankisense-docling-picture-description-api")
    stage = PictureDescriptionApiModel(enabled=True, enable_remote_services=True, artifacts_path=None,
        options=options, accelerator_options=AcceleratorOptions(num_threads=2, device="cpu"))
    stage.provenance = options.provenance
    document = DoclingDocument(name="AnkiSense selected picture check")
    item = document.add_picture()
    try:
        outputs = list(stage(document, [ItemAndImageEnrichmentElement(item=item, image=image)]))
    except Exception:
        return failure(diagnostics.get("error_code", "worker_error"))
    if diagnostics.get("error_code"):
        return failure(diagnostics["error_code"])
    finish = diagnostics.get("finish_reason")
    if finish == "length":
        return failure("truncated")
    if finish in {"content_filter", "tool_calls", "insufficient_system_resource", "aborted"}:
        return failure("generation_incomplete")
    if diagnostics.get("content_chars", 0) == 0:
        return failure("empty_response")
    if len(outputs) != 1 or not outputs[0].meta or not outputs[0].meta.description:
        return failure("invalid_response")
    description = outputs[0].meta.description
    text = description.text.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    diagnostics["response_chars"] = len(text)
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        return failure("invalid_json")
    usage = description.get_custom_part().get("docling__usage")
    # Forward only non-sensitive numeric billing counters, never provider blobs.
    usage = safe_usage(usage)
    return {"result": result, "usage": usage, "diagnostics": diagnostics, "engine": "docling-picture-description-api"}


def main():
    logging.disable(logging.CRITICAL)
    try:
        payload = json.loads(sys.stdin.read())
        result = enrich(payload)
    except Exception:
        # Upstream exceptions may contain request data. Never serialize them.
        result = {"error": "worker_error", "diagnostics": {"error_code": "worker_error"}}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
