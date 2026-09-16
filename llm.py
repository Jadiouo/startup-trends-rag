"""Single entry point for every LLM call in the project.

All generation goes through LiteLLM, so the model is just a string:

    ollama/llama3.1              local Ollama, no key needed (OLLAMA_API_BASE)
    openai/gpt-4o-mini           OpenAI (OPENAI_API_KEY)
    gemini/gemini-2.5-flash      Google AI Studio (GEMINI_API_KEY)
    anthropic/claude-sonnet-5    Anthropic (ANTHROPIC_API_KEY)
    openai/<name>                any OpenAI-compatible server (vLLM, LM Studio,
                                 a LiteLLM proxy, ...) via LLM_API_BASE + LLM_API_KEY

Configure with LLM_MODEL / LLM_API_KEY / LLM_API_BASE in .env (see .env.example).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from config import LLM_API_BASE, LLM_API_KEY, LLM_MODEL, OLLAMA_API_BASE

KEYLESS_PREFIXES = ("ollama/", "ollama_chat/")


@dataclass
class LLMConfig:
    model: str
    api_key: str | None
    api_base: str | None

    @property
    def ready(self) -> bool:
        """True when the model can be called: keyless providers, or a key is present."""
        return self.model.startswith(KEYLESS_PREFIXES) or bool(self.api_key)


def resolve(model: str | None = None) -> LLMConfig:
    """Work out key / base URL for a model string from the environment."""
    model = model or LLM_MODEL
    if model.startswith(KEYLESS_PREFIXES):
        return LLMConfig(model=model, api_key=None, api_base=OLLAMA_API_BASE)
    # provider-native keys (GEMINI_API_KEY, ANTHROPIC_API_KEY, ...) are read by
    # LiteLLM itself; LLM_API_KEY is the generic override used for OpenAI-compatible servers
    provider = model.split("/", 1)[0] if "/" in model else "openai"
    native_key = os.getenv(f"{provider.upper()}_API_KEY")
    return LLMConfig(model=model, api_key=LLM_API_KEY or native_key or None, api_base=LLM_API_BASE or None)


def chat(messages: list[dict], model: str | None = None, temperature: float = 0.3, stub: bool = False) -> str:
    """Return the assistant reply, or a bracketed [stub]/[error] string instead of raising."""
    cfg = resolve(model)
    if stub:
        return "[LLM stub — generation skipped]"
    if not cfg.ready:
        return (
            f"[LLM stub — no API key for {cfg.model}]\n"
            "Set LLM_API_KEY (or the provider's own *_API_KEY) in .env, "
            "or use a local model such as ollama/llama3.1."
        )
    try:
        import litellm  # heavy import, keep it lazy so --help / retrieval-only stay fast

        kwargs: dict = {"model": cfg.model, "messages": messages, "temperature": temperature}
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        if cfg.api_base:
            kwargs["api_base"] = cfg.api_base
        response = litellm.completion(**kwargs)
        return response.choices[0].message.content or ""
    except Exception as exc:  # network / auth / model errors are reported inline, not fatal
        return f"[LLM error: {exc}]"
