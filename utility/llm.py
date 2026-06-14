"""
utility/llm.py
─────────────────────────────────────────────────────────────────────────────
Generic LLM abstraction layer. Single function for all LLM calls across
the codebase. Switches between providers based on LLM_PROVIDER config.

Supported providers:
    ollama  → Local Ollama (dev default, no API key needed)
    openai  → Any OpenAI-compatible gateway:
                - Portkey (https://api.portkey.ai/v1)
                - Azure OpenAI (https://your-resource.openai.azure.com/)
                - LiteLLM (http://litellm:8080/v1)
                - Any other OpenAI-compatible endpoint

Usage:
    from utility.llm import llm_chat

    response = llm_chat(
        messages=[{"role": "user", "content": "What is my PCP copay?"}],
        format="json",       # optional: "json" for structured output
        max_tokens=100,      # optional: override default max tokens
        temperature=0.0,     # optional: override default temperature
    )
    # response is always a plain string

Configuration (.env):
    # Local dev
    LLM_PROVIDER=ollama
    OLLAMA_MODEL=llama3.1

    # Portkey (production)
    LLM_PROVIDER=openai
    LLM_BASE_URL=https://api.portkey.ai/v1
    LLM_API_KEY=your-portkey-api-key
    LLM_MODEL=gpt-4o-mini
    LLM_VIRTUAL_KEY=your-portkey-virtual-key   # routes to Azure/Anthropic/etc.

    # Azure OpenAI direct (no gateway)
    LLM_PROVIDER=openai
    LLM_BASE_URL=https://your-resource.openai.azure.com/
    LLM_API_KEY=your-azure-api-key
    LLM_MODEL=gpt-4o-mini

    # LiteLLM or any other OpenAI-compatible gateway
    LLM_PROVIDER=openai
    LLM_BASE_URL=http://litellm:8080/v1
    LLM_API_KEY=your-key
    LLM_MODEL=azure/gpt-4o-mini
"""

import json
import ollama
from config import settings

# ── Token usage tracking ──────────────────────────────────────────────────────
# Tracks all LLM calls in the current process session.
# Reset between requests by calling reset_token_log().
# Access summary via get_token_summary().

_token_log: list[dict] = []


def reset_token_log() -> None:
    """Reset token log — call at start of each request."""
    global _token_log
    _token_log = []


def log_tokens(call_type: str, input_tokens: int, output_tokens: int) -> None:
    """Record token usage for one LLM call."""
    _token_log.append(
        {
            "call_type": call_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
    )
    print(
        f"[TOKENS] {call_type}: in={input_tokens} out={output_tokens} total={input_tokens + output_tokens}"
    )


def get_token_summary() -> dict:
    """
    Returns token usage summary for the current request.
    Call after get_ai_response() to get per-query token counts.
    """
    total_input = sum(t["input_tokens"] for t in _token_log)
    total_output = sum(t["output_tokens"] for t in _token_log)
    return {
        "total_llm_calls": len(_token_log),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "calls": _token_log,
    }


def llm_chat(
    messages: list,
    format: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.0,
    tools: list | None = None,
) -> str:
    """
    Send a chat request to the configured LLM provider.

    Always returns a plain string — the model's response content.
    Returns empty string on any failure (silent fail).

    Args:
        messages:    List of {"role": "user/assistant/system", "content": "..."}
        format:      "json" to request JSON output
        max_tokens:  Override default max tokens from settings
        temperature: Sampling temperature (0.0 = deterministic)
        tools:       Tool definitions for function calling (optional)

    Returns:
        str: Model response text, or "" on failure
    """
    tokens = max_tokens or settings.LLM_MAX_TOKENS

    try:
        if settings.LLM_PROVIDER == "openai":
            return _call_openai(messages, tokens, temperature, format, tools)
        else:
            return _call_ollama(messages, tokens, temperature, format, tools)

    except Exception as e:
        print(f"[!] LLM call failed ({settings.LLM_PROVIDER}): {e}")
        return ""


def llm_generate(prompt: str, max_tokens: int = 60, temperature: float = 0.0) -> str:
    """
    Simple single-prompt generation (no chat history).
    Used for mini-LLM calls where we just need a quick JSON response.

    Returns plain string response or "" on failure.
    """
    return llm_chat(
        messages=[{"role": "user", "content": prompt}],
        format="json",
        max_tokens=max_tokens,
        temperature=temperature,
    )


# ── Provider implementations ──────────────────────────────────────────────────


def _call_ollama(
    messages: list,
    max_tokens: int,
    temperature: float,
    format: str | None,
    tools: list | None = None,
) -> str:
    """
    Call local Ollama instance.
    Uses ollama library directly — no API key needed.
    """
    options = {
        "temperature": temperature,
        "num_predict": max_tokens,
    }

    from typing import Literal

    fmt: Literal["", "json"] = "json" if format == "json" else ""

    response = ollama.chat(  # type: ignore[call-overload]
        model=settings.OLLAMA_MODEL,
        messages=messages,
        format=fmt,
        options=options,
    )

    # Ollama returns token counts in the response
    input_tokens = response.get("prompt_eval_count", 0)
    output_tokens = response.get("eval_count", 0)
    log_tokens("ollama", input_tokens, output_tokens)

    return response["message"]["content"]


def _call_openai(
    messages: list,
    max_tokens: int,
    temperature: float,
    format: str | None,
    tools: list | None = None,
) -> str:
    """
    Call any OpenAI-compatible gateway.
    Works for Portkey, Azure OpenAI, LiteLLM, or any OpenAI-compatible API.

    Headers are built dynamically:
        - x-portkey-virtual-key: added when LLM_VIRTUAL_KEY is set (Portkey routing)
        - Any other gateway-specific headers can be added here
    """
    from openai import OpenAI

    # Build optional gateway-specific headers
    extra_headers = {}
    if settings.LLM_VIRTUAL_KEY:
        # Portkey uses virtual keys to route to the actual provider
        extra_headers["x-portkey-virtual-key"] = settings.LLM_VIRTUAL_KEY

    client = OpenAI(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
        default_headers=extra_headers if extra_headers else None,
    )

    # Build request kwargs
    kwargs: dict = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    # Request JSON output when format="json"
    if format == "json":
        kwargs["response_format"] = {"type": "json_object"}

    response = client.chat.completions.create(**kwargs)

    # OpenAI returns token counts in usage
    if response.usage:
        log_tokens(
            "openai", response.usage.prompt_tokens, response.usage.completion_tokens
        )

    return response.choices[0].message.content or ""


def llm_chat_with_tools(messages: list, tools: list, temperature: float = 0.0) -> dict:
    """
    Tool calling variant — returns full response dict with content and tool_calls.
    Works with both Ollama (dev) and OpenAI-compatible gateways (prod).

    Returns:
        dict: {"content": str, "tool_calls": list}
    """
    try:
        if settings.LLM_PROVIDER == "openai":
            from openai import OpenAI
            import json as _json

            extra_headers = {}
            if settings.LLM_VIRTUAL_KEY:
                extra_headers["x-portkey-virtual-key"] = settings.LLM_VIRTUAL_KEY

            client = OpenAI(
                base_url=settings.LLM_BASE_URL,
                api_key=settings.LLM_API_KEY,
                default_headers=extra_headers if extra_headers else None,
            )
            response = client.chat.completions.create(
                model=settings.LLM_MODEL,
                messages=messages,
                tools=tools,
                temperature=temperature,
            )
            msg = response.choices[0].message
            tool_calls = []
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls.append(
                        {
                            "function": {
                                "name": tc.function.name,  # type: ignore[union-attr]
                                "arguments": _json.loads(tc.function.arguments),  # type: ignore[union-attr]
                            }
                        }
                    )
            return {"content": msg.content or "", "tool_calls": tool_calls}

        else:
            # Ollama tool calling
            resp = ollama.chat(  # type: ignore[call-overload]
                model=settings.OLLAMA_MODEL,
                messages=messages,
                tools=tools,
                options={"temperature": temperature},
            )
            msg = resp["message"]
            return {
                "content": msg.get("content", ""),
                "tool_calls": msg.get("tool_calls", []),
            }

    except Exception as e:
        print(f"[!] LLM tool call failed ({settings.LLM_PROVIDER}): {e}")
        return {"content": "", "tool_calls": []}
