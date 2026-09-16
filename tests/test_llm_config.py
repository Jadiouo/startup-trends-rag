"""Pure tests for provider resolution in llm.py (no network, no DB)."""
import importlib
import sys


def load(monkeypatch, **env):
    # set to "" rather than delete: load_dotenv() never overrides variables that
    # already exist, so a developer's real .env cannot leak into the tests
    for k in ("LLM_MODEL", "LLM_API_KEY", "LLM_API_BASE", "OPENAI_API_KEY", "OPENAI_API_BASE",
              "LITELLM_API_KEY", "LITELLM_BASE_URL", "RAG_DEFAULT_MODEL", "GEMINI_API_KEY"):
        monkeypatch.setenv(k, "")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for m in ("config", "llm"):
        sys.modules.pop(m, None)
    return importlib.import_module("llm")


def test_ollama_needs_no_key(monkeypatch):
    llm = load(monkeypatch, LLM_MODEL="ollama/llama3.1")
    cfg = llm.resolve()
    assert cfg.ready and cfg.api_key is None and cfg.api_base == "http://localhost:11434"


def test_openai_compatible_server(monkeypatch):
    llm = load(monkeypatch, LLM_MODEL="openai/gpt-oss:20b", LLM_API_KEY="k", LLM_API_BASE="https://proxy.example")
    cfg = llm.resolve()
    assert cfg.ready and cfg.api_key == "k" and cfg.api_base == "https://proxy.example"


def test_legacy_variable_names_still_work(monkeypatch):
    llm = load(monkeypatch, RAG_DEFAULT_MODEL="openai/gemma4", OPENAI_API_KEY="legacy", OPENAI_API_BASE="https://old")
    cfg = llm.resolve()
    assert cfg.model == "openai/gemma4" and cfg.api_key == "legacy" and cfg.api_base == "https://old"


def test_provider_native_key(monkeypatch):
    llm = load(monkeypatch, LLM_MODEL="gemini/gemini-2.5-flash", GEMINI_API_KEY="g")
    assert llm.resolve().ready


def test_missing_key_gives_stub_not_exception(monkeypatch):
    llm = load(monkeypatch, LLM_MODEL="openai/gpt-4o-mini")
    reply = llm.chat([{"role": "user", "content": "hi"}])
    assert reply.startswith("[LLM stub")


def test_explicit_model_overrides_env(monkeypatch):
    llm = load(monkeypatch, LLM_MODEL="ollama/llama3.1")
    assert llm.resolve("openai/x").model == "openai/x"
