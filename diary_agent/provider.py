from __future__ import annotations

import json
import re

from openai import OpenAI


class ChatProvider:
    def __init__(self, *, api_key: str, base_url: str, model: str):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def chat(self, messages: list[dict[str, str]]) -> str:
        response = self.client.chat.completions.create(model=self.model, messages=messages)
        return response.choices[0].message.content or ""

    def json(self, messages: list[dict[str, str]]) -> dict:
        request = {"model": self.model, "messages": messages}
        try:
            response = self.client.chat.completions.create(
                **request, response_format={"type": "json_object"}
            )
        except Exception:
            response = self.client.chat.completions.create(**request)
        content = response.choices[0].message.content or "{}"
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", content, re.S)
        return json.loads(fenced.group(1) if fenced else content)

