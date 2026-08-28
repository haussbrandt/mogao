import asyncio
import json
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
INITIAL_RETRY_DELAY_SECONDS = 5.0
MAX_RETRY_DELAY_SECONDS = 60.0


class LLMResponseError(RuntimeError):
    """Raised when an LLM endpoint returns an unexpected response."""


def _raise_for_status(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise LLMResponseError(
            f"LLM API returned HTTP {response.status_code}. Response body:\n"
            f"{response.text}"
        ) from error


def _chat_completions_url(base_url: str) -> str:
    url = base_url.strip().rstrip("/")
    if not url:
        raise ValueError("LLM base URL cannot be empty")
    if url.endswith("/chat/completions"):
        return url
    return f"{url}/chat/completions"


def _retry_delay(response: requests.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), MAX_RETRY_DELAY_SECONDS)
            except ValueError:
                pass
    return min(
        INITIAL_RETRY_DELAY_SECONDS * (2**attempt),
        MAX_RETRY_DELAY_SECONDS,
    )


async def _post_with_retries(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> requests.Response:
    for attempt in range(MAX_ATTEMPTS):
        response = None
        try:
            response = await asyncio.to_thread(
                requests.post,
                url,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except (requests.ConnectionError, requests.Timeout) as error:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            reason = type(error).__name__
        else:
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable:
                _raise_for_status(response)
                return response
            if attempt == MAX_ATTEMPTS - 1:
                _raise_for_status(response)
            reason = f"HTTP {response.status_code}"

        delay = _retry_delay(response, attempt)
        logger.warning(
            "LLM request failed with %s; retrying in %.1f seconds",
            reason,
            delay,
        )
        await asyncio.sleep(delay)

    raise RuntimeError("LLM retry loop ended unexpectedly")


def _parse_json_content(content: str) -> Any:
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 : -3].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        logger.warning("Could not decode LLM response as JSON: %s", error)
        logger.info("Retrying LLM response with raw_decode")
        data, _ = json.JSONDecoder().raw_decode(text)
        return data


async def generate_json(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    response_schema: dict[str, Any],
    schema_name: str,
    timeout: float,
    temperature: float | None = None,
) -> Any:
    """Generate structured JSON through an OpenAI-compatible REST endpoint."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": response_schema,
            },
        },
    }
    if temperature is not None:
        payload["temperature"] = temperature

    response = await _post_with_retries(
        _chat_completions_url(base_url),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload=payload,
        timeout=timeout,
    )

    try:
        response_data = response.json()
        content = response_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise LLMResponseError(
            f"LLM API returned HTTP {response.status_code} with an invalid "
            f"response body:\n{response.text}"
        ) from error

    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise LLMResponseError("LLM endpoint returned no text content")

    return _parse_json_content(content)
