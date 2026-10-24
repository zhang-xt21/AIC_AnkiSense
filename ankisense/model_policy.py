"""Provider-specific request options shared by text and vision adapters."""

from urllib.parse import urlsplit


def generation_options(base_url: str, model: str) -> dict:
    """Disable thinking where the provider documents this extension.

    An OpenAI-compatible endpoint does not imply DeepSeek-compatible options.
    Keep other providers and proxies unchanged, without probing or retrying a
    paid request to discover support.
    """
    try:
        endpoint = urlsplit(base_url)
        official = (endpoint.scheme == "https" and endpoint.hostname == "api.deepseek.com"
                    and endpoint.port in {None, 443} and not endpoint.username and not endpoint.password)
    except ValueError:
        return {}
    if official and model in {"deepseek-flash", "deepseek-v4-pro"}:
        return {"thinking": {"type": "disabled"}}
    return {}
