#!/usr/bin/env python3
"""Translate Japanese lyric lines with an OpenAI-compatible chat API.

The module keeps timestamps, metadata, line order, and non-Japanese lines in
the local process. Only timed lines containing Japanese kana are sent to the
API, and the response must be a JSON array with the same number of entries.

The API endpoint can be changed with the ``OPENAI_BASE_URL`` environment
variable (OpenAI-compatible chat completions endpoint). This allows cheap
models such as DeepSeek or a local gateway to be used for lyric translation.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any
from token_usage import record_usage


JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
TIMED_LINE_RE = re.compile(r"^(?P<tags>(?:\[\d+:\d+(?:[.:]\d+)?\])+)(?P<text>.*)$")
METADATA_RE = re.compile(r"(?:作詞|作曲|編曲|歌詞|翻訳|唄|演奏|lyrics|lyricist|composer|arranged)", re.I)

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


class TranslationError(RuntimeError):
    """Raised when a translation request or its validation fails."""


def japanese_lines(text: str) -> list[str]:
    """Return timed lyric texts that are safe candidates for translation."""
    candidates: list[str] = []
    for line in text.splitlines():
        match = TIMED_LINE_RE.match(line)
        if not match:
            continue
        lyric = match.group("text")
        if lyric.strip() and JAPANESE_RE.search(lyric) and not METADATA_RE.search(lyric):
            candidates.append(lyric)
    return candidates


def _response_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
    raise TranslationError("model response did not contain text output")


def _parse_array(raw: str, expected: int) -> list[str]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I | re.S).strip()
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise TranslationError("model returned invalid JSON instead of a lyric array") from exc
    if not isinstance(result, list) or len(result) != expected or not all(
        isinstance(item, str) for item in result
    ):
        raise TranslationError(f"model returned {len(result) if isinstance(result, list) else 'non-array'} lines; expected {expected}")
    if any("\n" in item or "\r" in item or TIMED_LINE_RE.match(item) for item in result):
        raise TranslationError("model returned a line containing a newline or timestamp")
    return result


def translate_lines(
    lines: list[str],
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str | None = None,
    timeout: int = 120,
) -> list[str]:
    """Translate lyric text entries while preserving one output per entry."""
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise TranslationError("OPENAI_API_KEY is required for --translate-japanese")
    if not lines:
        return []

    prompt = (
        "Translate each Japanese lyric line into natural Simplified Chinese. "
        "Return only a JSON array of strings with exactly the same length and order. "
        "Do not add explanations, timestamps, quotation marks, or markdown. "
        "Preserve embedded English words and punctuation where natural.\n\n"
        + json.dumps(lines, ensure_ascii=False)
    )
    request_body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You translate Japanese song lyrics accurately and concisely.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    base = (base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        record_usage(None, model=model, source="lyrics")
        detail = getattr(exc, "reason", str(exc))
        raise TranslationError(f"translation request failed: {detail}") from exc
    if not isinstance(payload, dict):
        record_usage(None, model=model, source="lyrics")
        raise TranslationError("model returned an unexpected response")
    record_usage(payload.get("usage"), model=payload.get("model") or model, source="lyrics")
    return _parse_array(_response_text(payload), len(lines))


def translate_lrc(
    text: str,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str | None = None,
    timeout: int = 120,
) -> str:
    """Translate Japanese timed lines and leave all other LRC content intact."""
    candidates = japanese_lines(text)
    if not candidates:
        return text
    translations = iter(
        translate_lines(candidates, api_key=api_key, model=model, base_url=base_url, timeout=timeout)
    )
    candidate_indexes = {
        index
        for index, line in enumerate(text.splitlines())
        if (match := TIMED_LINE_RE.match(line))
        and match.group("text") in candidates
        and JAPANESE_RE.search(match.group("text"))
        and not METADATA_RE.search(match.group("text"))
    }
    output: list[str] = []
    for index, line in enumerate(text.splitlines()):
        match = TIMED_LINE_RE.match(line)
        if index in candidate_indexes and match:
            replacement = next(translations)
            output.append(match.group("tags") + replacement)
        else:
            output.append(line)
    return "\n".join(output) + ("\n" if text.endswith(("\n", "\r")) else "")
