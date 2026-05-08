"""
OllamaLLMAdapter — satisfies plan_indexer.LLMProtocol using the local ollama library.

Usage (in indexer runner scripts):
    from app.llm_adapter import OllamaLLMAdapter
    from plan_indexer import PlanIndexer

    llm = OllamaLLMAdapter()
    indexer = PlanIndexer(llm=llm, ...)
    indexer.build_all()

PAAI users can substitute any object whose .invoke(messages, **kwargs) -> str
instead — no code change needed in plan_indexer itself.
"""

import os
import json
import ollama


class OllamaLLMAdapter:
    """Minimal adapter: wraps ollama so it satisfies plan_indexer.LLMProtocol."""

    def __init__(self, model: str | None = None):
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3.1")

    def invoke(self, messages: list[dict], **kwargs) -> str:
        """Send messages to ollama and return the text response."""
        # plan_indexer passes a single user message; format="json" gives structured output.
        prompt = messages[-1]["content"] if messages else ""
        response = ollama.generate(
            model=self.model,
            prompt=prompt,
            format="json",
            options={"temperature": 0},
        )
        return response["response"]
