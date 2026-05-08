"""
Structural Protocol for the LLM dependency.

plan_indexer uses structural (duck-type) subtyping via typing.Protocol so
that the library has zero hard dependency on PAAI or any other LLM framework.

Any object that exposes:
    invoke(messages: list[dict], **kwargs) -> str

satisfies LLMProtocol automatically — including PAAI's BaseLLM.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProtocol(Protocol):
    """Minimal LLM interface required by plan_indexer."""

    def invoke(self, messages: list[dict], **kwargs) -> str:
        """Send messages and return the model's text response."""
        ...
