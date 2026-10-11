"""Anthropic adapter against a mock HTTP transport (no network calls)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from app.config import LLMProviderName, Settings
from app.conversation.tools import TOOLS
from app.http import ExternalServiceError
from app.llm.anthropic import AnthropicClient
from app.llm.base import (
    TextBlock,
    ToolCall,
    ToolResult,
    Turn,
    UnsupportedProviderError,
    build_llm_client,
)


def _message(content: list[dict[str, Any]], stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 5200,
            "output_tokens": 40,
            "cache_read_input_tokens": 4800,
            "cache_creation_input_tokens": 0,
        },
    }


def _client(
    settings: Settings, handler: Callable[[httpx2.Request], httpx2.Response]
) -> AnthropicClient:
    sdk = anthropic.Anthropic(
        api_key="test-llm-key",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicClient(settings, client=sdk)


def test_tool_round_trip_request_shape(settings: Settings) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json=_message(
                [
                    {"type": "text", "text": "Let me check."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_availability",
                        "input": {"service_id": "cleaning", "date_from": "2026-10-05"},
                    },
                ],
                stop_reason="tool_use",
            ),
        )

    history = [
        Turn.text("user", "hi"),
        Turn("assistant", (ToolCall("toolu_0", "list_customer_bookings", {}),)),
        Turn("user", (ToolResult("toolu_0", '{"bookings": []}'),)),
    ]
    reply = _client(settings, handler).complete("SYSTEM", history, TOOLS)

    assert reply.text == "Let me check."
    assert reply.tool_calls == [
        ToolCall(
            "toolu_1", "get_availability", {"service_id": "cleaning", "date_from": "2026-10-05"}
        )
    ]
    assert (reply.input_tokens, reply.cache_read_tokens) == (5200, 4800)

    body = seen[0]
    assert body["model"] == settings.llm_model
    assert body["max_tokens"] == settings.llm_max_output_tokens
    assert body["system"] == [
        {"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}
    ]
    assert [t["name"] for t in body["tools"]] == [t.name for t in TOOLS]
    assert body["messages"][1]["content"] == [
        {"type": "tool_use", "id": "toolu_0", "name": "list_customer_bookings", "input": {}}
    ]
    assert body["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_0", "content": '{"bookings": []}'}
    ]


def test_error_results_are_flagged(settings: Settings) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=_message([{"type": "text", "text": "ok"}]))

    history = [
        Turn.text("user", "hi"),
        Turn("assistant", (ToolCall("t", "cancel_booking", {"booking_id": 9}),)),
        Turn("user", (ToolResult("t", "not found", is_error=True), TextBlock(""))),
    ]
    _client(settings, handler).complete("S", history, [])
    assert seen[0]["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "t", "content": "not found", "is_error": True}
    ]
    assert "tools" not in seen[0]


@pytest.mark.parametrize(
    ("status", "body", "match"),
    [
        (500, {"type": "error", "error": {"type": "api_error", "message": "boom"}}, "HTTP 500"),
        (200, _message([], stop_reason="end_turn"), "empty reply"),
        (200, _message([], stop_reason="refusal"), "refusal"),
    ],
)
def test_failures_become_external_service_errors(
    settings: Settings, status: int, body: dict[str, Any], match: str
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json=body)

    with pytest.raises(ExternalServiceError, match=match):
        _client(settings, handler).complete("S", [Turn.text("user", "hi")], TOOLS)


def test_unimplemented_provider_fails_at_startup(settings: Settings) -> None:
    other = settings.model_copy(update={"llm_provider": LLMProviderName.OPENAI})
    with pytest.raises(UnsupportedProviderError, match="openai"):
        build_llm_client(other)
