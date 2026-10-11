"""Anthropic adapter for ``LLMClient``, using the official SDK's Messages API.

* Tools are sent on every call, in a fixed order, followed by the system
  prompt, which carries the ``cache_control`` marker. The cached prefix is
  therefore tools + system (knowledge base included). Caching starts once that
  prefix passes the model's minimum size (4,096 tokens on Haiku 4.5), which a
  full 60-Q&A knowledge base does. Check ``cache_read_tokens`` in the logs.
* Anything that changes per request (the current time, the customer's name)
  lives in the last user message, never in the system prompt.
* Timeouts and retries come from the SDK (it retries 408/409/429/5xx and
  connection errors). SDK errors become ``ExternalServiceError``, so the
  conversation layer handles every provider the same way.
"""

from __future__ import annotations

from typing import Any

import anthropic
from anthropic.types import MessageParam, TextBlockParam, ToolParam

from app.config import Settings
from app.http import ExternalServiceError
from app.llm.base import LLMReply, TextBlock, ToolCall, ToolResult, ToolSpec, Turn

# A step normally takes 1-4 s. The timeout sits well above that and well below
# "customer gives up"; a stalled request is retried quickly.
TIMEOUT = anthropic.Timeout(20.0, connect=5.0)
MAX_RETRIES = 2


def _content(turn: Turn) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for b in turn.blocks:
        if isinstance(b, TextBlock):
            if b.text:
                blocks.append({"type": "text", "text": b.text})
        elif isinstance(b, ToolCall):
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif isinstance(b, ToolResult):
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": b.tool_call_id,
                    "content": b.content,
                    **({"is_error": True} if b.is_error else {}),
                }
            )
    return blocks


class AnthropicClient:
    """Implements ``app.llm.base.LLMClient`` for Anthropic."""

    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None) -> None:
        self._model = settings.llm_model
        self._max_tokens = settings.llm_max_output_tokens
        self._client = client or anthropic.Anthropic(
            api_key=settings.llm_api_key.get_secret_value(),
            timeout=TIMEOUT,
            max_retries=MAX_RETRIES,
        )

    def complete(self, system: str, history: list[Turn], tools: list[ToolSpec]) -> LLMReply:
        """See ``LLMClient.complete``."""
        system_blocks: list[TextBlockParam] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        messages: list[MessageParam] = [
            {"role": t.role, "content": _content(t)}  # type: ignore[typeddict-item]
            for t in history
        ]
        tool_params: list[ToolParam] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system_blocks,
                messages=messages,
                tools=tool_params if tool_params else anthropic.omit,
            )
        except anthropic.APIStatusError as exc:
            raise ExternalServiceError(
                "anthropic", f"HTTP {exc.status_code}", exc.status_code
            ) from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise ExternalServiceError("anthropic", type(exc).__name__) from exc

        if response.stop_reason == "refusal":
            raise ExternalServiceError("anthropic", "refusal")
        blocks: list[TextBlock | ToolCall] = []
        for block in response.content:
            if block.type == "text" and block.text.strip():
                blocks.append(TextBlock(block.text))
            elif block.type == "tool_use":
                blocks.append(ToolCall(block.id, block.name, dict(block.input)))
        if not blocks:
            raise ExternalServiceError("anthropic", f"empty reply ({response.stop_reason})")
        usage = response.usage
        return LLMReply(
            blocks=tuple(blocks),
            stop_reason=response.stop_reason or "",
            model=response.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_write_tokens=usage.cache_creation_input_tokens or 0,
        )
