import logging
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)


def build_provider_headers(api_key: str = "") -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/Koshigawarman/R26-SE-029",
        "X-Title": "AI Backend Builder",
    }

    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

        if api_key.startswith("wk-") and ".ws-" in api_key:
            token_id, token_secret = api_key.split(".", 1)
            headers["Modal-Key"] = token_id
            headers["Modal-Secret"] = token_secret

    return headers


def raise_for_provider_error(response: requests.Response, provider: str, url: str) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body = response.text[:2000] if response.text else ""
        logger.error(
            "OpenAI-compatible provider error | provider=%s status=%s url=%s body=%s",
            provider,
            response.status_code,
            url,
            body,
        )
        raise RuntimeError(
            f"{provider} returned HTTP {response.status_code} for {url}. "
            f"Response body: {body or '(empty)'}"
        ) from exc
