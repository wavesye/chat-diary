from __future__ import annotations

import json
import re
import time

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)


class ProviderResponseError(RuntimeError):
    """The model replied, but the payload could not be used by the application."""


class ChatProvider:
    def __init__(self, *, api_key: str, base_url: str, model: str):
        # Keep retry behavior here so all compatible providers follow the same
        # policy instead of stacking SDK retries with application retries.
        self.client = OpenAI(
            api_key=api_key, base_url=base_url, max_retries=0
        )
        self.model = model

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        if isinstance(
            error,
            (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError),
        ):
            return True
        if isinstance(error, APIStatusError):
            return error.status_code == 429 or error.status_code >= 500
        return False

    def _create(self, **request):
        """Retry only failures that are safe and likely to be temporary."""
        delays = (1, 2)
        for attempt in range(len(delays) + 1):
            try:
                return self.client.chat.completions.create(**request)
            except Exception as error:
                if attempt >= len(delays) or not self._is_retryable(error):
                    raise
                time.sleep(delays[attempt])
        raise AssertionError("unreachable")

    def chat(self, messages: list[dict[str, str]]) -> str:
        response = self._create(model=self.model, messages=messages)
        content = response.choices[0].message.content
        if not content or not content.strip():
            raise ProviderResponseError("模型没有返回回复内容")
        return content

    def json(self, messages: list[dict[str, str]]) -> dict:
        request = {"model": self.model, "messages": messages}
        try:
            response = self._create(
                **request, response_format={"type": "json_object"}
            )
        except BadRequestError:
            # Some OpenAI-compatible services do not implement response_format.
            # Only fall back for that request error. Connection failures keep their
            # real meaning and use the retry path above.
            response = self._create(**request)
        content = response.choices[0].message.content
        if not content or not content.strip():
            raise ProviderResponseError("模型没有返回结构化内容")
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", content, re.S)
        try:
            result = json.loads(fenced.group(1) if fenced else content)
        except json.JSONDecodeError as error:
            raise ProviderResponseError("模型返回的结构化内容不是有效 JSON") from error
        if not isinstance(result, dict):
            raise ProviderResponseError("模型返回的结构化内容不是 JSON 对象")
        return result
