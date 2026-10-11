"""LLM interface shared by all provider adapters (CLAUDE.md §3).

The conversation layer only talks to ``LLMClient``, so switching provider is a
config change (``LLM_PROVIDER``) plus one adapter file, not a rewrite. The
types here are provider-neutral: a turn is a list of blocks, where a block is
text, a tool call made by the model, or the result we send back for it.

Only the Anthropic adapter is built. The brief requires the client's chosen
provider; picking one without an adapter fails at startup, not mid-conversation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.config import LLMProviderName, Settings


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    """The model asking us to run a tool."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Our answer to a ``ToolCall``, sent back in the next user turn."""

    tool_call_id: str
    content: str
    is_error: bool = False


Block = TextBlock | ToolCall | ToolResult


@dataclass(frozen=True, slots=True)
class Turn:
    """One message of the conversation sent to the model."""

    role: Literal["user", "assistant"]
    blocks: tuple[Block, ...]

    @classmethod
    def text(cls, role: Literal["user", "assistant"], text: str) -> Turn:
        return cls(role, (TextBlock(text),))


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool the model may call. ``input_schema`` is JSON Schema."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMReply:
    """Model output plus token usage, which is logged to watch running cost."""

    blocks: tuple[TextBlock | ToolCall, ...]
    stop_reason: str = ""  # "tool_use" | "end_turn" | "max_tokens" | ...
    model: str = ""  # model id the provider actually served
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks if isinstance(b, TextBlock)).strip()

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [b for b in self.blocks if isinstance(b, ToolCall)]


class LLMClient(Protocol):
    """What every provider adapter implements."""

    def complete(self, system: str, history: list[Turn], tools: list[ToolSpec]) -> LLMReply:
        """Run one model step.

        Args:
            system: System prompt (persona, rules, knowledge). It must be
                byte-stable between calls so the provider can cache it.
            history: The conversation, starting with a user turn and ending
                with a user turn (a message or tool results).
            tools: Tools the model may call. Keep the list and its order fixed,
                since it is part of the cached prefix.

        Raises:
            app.http.ExternalServiceError: The provider failed, refused, or
                returned neither text nor a tool call.
        """
        ...


class UnsupportedProviderError(ValueError):
    """``LLM_PROVIDER`` names a provider that has no adapter in this build."""


def build_llm_client(settings: Settings) -> LLMClient:
    """Return the adapter selected by ``LLM_PROVIDER``.

    Raises:
        UnsupportedProviderError: No adapter is implemented for that provider.
    """
    if settings.llm_provider is LLMProviderName.ANTHROPIC:
        from app.llm.anthropic import AnthropicClient

        return AnthropicClient(settings)
    raise UnsupportedProviderError(
        f"LLM_PROVIDER={settings.llm_provider.value} has no adapter yet; use 'anthropic'"
    )
