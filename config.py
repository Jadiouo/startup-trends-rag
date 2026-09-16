"""Central configuration for startup-trends-rag.

All tunable parameters live here. Scripts import from this module;
no magic numbers scattered in application code.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).parent
DATA_DIR = ROOT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
CRAWLED_DIR = RAW_DIR / "crawled"
MANUAL_DIR = RAW_DIR / "manual"
PROCESSED_DIR = DATA_DIR / "processed"

for _d in (CRAWLED_DIR, MANUAL_DIR, PROCESSED_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------
EMBEDDING_MODEL: str = os.getenv(
    "EMBEDDING_MODEL", "sentence-transformers/all-mpnet-base-v2"
)
EMBEDDING_DIM: int = int(os.getenv("EMBEDDING_DIM", "768"))

# ---------------------------------------------------------------------------
# pgvector / Postgres
# ---------------------------------------------------------------------------
PGVECTOR_HOST: str = os.getenv("PGVECTOR_HOST", "localhost")
PGVECTOR_PORT: int = int(os.getenv("PGVECTOR_PORT", "5432"))
PGVECTOR_USER: str = os.getenv("PGVECTOR_USER", "raguser")
PGVECTOR_PASSWORD: str = os.getenv("PGVECTOR_PASSWORD", "ragpassword")
PGVECTOR_DB: str = os.getenv("PGVECTOR_DB", "ragdb")

PG_DSN: str = (
    f"postgresql://{PGVECTOR_USER}:{PGVECTOR_PASSWORD}"
    f"@{PGVECTOR_HOST}:{PGVECTOR_PORT}/{PGVECTOR_DB}"
)

# ---------------------------------------------------------------------------
# LLM (any provider LiteLLM supports; see llm.py)
#   LLM_MODEL     e.g. ollama/llama3.1, openai/gpt-4o-mini, gemini/gemini-2.5-flash,
#                 or openai/<name> against an OpenAI-compatible server
#   LLM_API_KEY   generic key (OPENAI_API_KEY / LITELLM_API_KEY still accepted)
#   LLM_API_BASE  base URL for OpenAI-compatible servers (OPENAI_API_BASE / LITELLM_BASE_URL accepted)
# ---------------------------------------------------------------------------
LLM_MODEL: str = (
    os.getenv("LLM_MODEL")
    or os.getenv("RAG_DEFAULT_MODEL")
    or "ollama/llama3.1"
)
LLM_API_KEY: str = (
    os.getenv("LLM_API_KEY")
    or os.getenv("OPENAI_API_KEY")
    or os.getenv("LITELLM_API_KEY")
    or ""
)
LLM_API_BASE: str = (
    os.getenv("LLM_API_BASE")
    or os.getenv("OPENAI_API_BASE")
    or os.getenv("LITELLM_BASE_URL")
    or ""
)
OLLAMA_API_BASE: str = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")

# Backward-compatible aliases
LITELLM_API_KEY = LLM_API_KEY
LITELLM_BASE_URL = LLM_API_BASE

# ---------------------------------------------------------------------------
# RAG defaults
# ---------------------------------------------------------------------------
RAG_TOP_K: int = int(os.getenv("RAG_TOP_K", "5"))
RAG_DEFAULT_MODEL: str = LLM_MODEL
RAG_TEMPERATURE: float = 0.3
RAG_MAX_HISTORY: int = 5  # multi-turn window (turns kept)

# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
CHUNK_SIZE: int = 800
CHUNK_OVERLAP: int = 100
CHUNK_SEPARATORS: list[str] = ["\n\n", "\n", ". ", "? ", "! ", " ", ""]

# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------
CRAWLER_CONCURRENCY: int = 5
CRAWLER_DELAY: float = 1.0  # seconds between requests per domain
CRAWLER_TIMEOUT: int = 30
CRAWLER_RETRY_ATTEMPTS: int = 3
CRAWLER_MIN_DATE: str = "2024-01-01"  # filter articles before this date

POLITE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; academic-research-bot/1.0; "
        "+https://github.com/Jadiouo/startup-trends-rag)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Whitelist of RSS-based sources
RSS_SOURCES: list[dict] = [
    {
        "name": "tomtunguz",
        "url": "https://tomtunguz.com",
        "rss": "https://tomtunguz.com/index.xml",
        "topic": "SaaS metrics, AI investment data",
    },
    {
        "name": "avc",
        "url": "https://avc.com",
        "rss": "https://avc.com/feed/",
        "topic": "Early-stage investment perspective",
    },
    {
        "name": "bothsidesofthetable",
        "url": "https://bothsidesofthetable.com",
        "rss": "https://bothsidesofthetable.com/feed/",
        "topic": "VC fundraising, founder advice",
    },
    {
        "name": "firstround",
        "url": "https://review.firstround.com",
        "rss": "https://review.firstround.com/feed/",
        "topic": "Founder deep-dives",
    },
    {
        "name": "a16z",
        "url": "https://a16z.com/news-content/",
        "rss": "https://a16z.com/wp-json/wp/v2/posts?per_page=20&_fields=link,date,title",
        "topic": "Sector trend reports",
        "type": "json_api",
    },
    {
        "name": "nfx",
        "url": "https://www.nfx.com/post",
        "rss": "https://www.nfx.com/rss",
        "topic": "Network effects, new sectors",
    },
    {
        "name": "saastr",
        "url": "https://www.saastr.com",
        "rss": "https://www.saastr.com/feed/",
        "topic": "SaaS early growth",
    },
]

# Paul Graham — no RSS, parse HTML index
PG_INDEX_URL: str = "https://paulgraham.com/articles.html"
