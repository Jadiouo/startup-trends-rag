"""rag_query.py — Retrieval-augmented query interface for startup-trends-rag.

Supports single-shot queries and interactive multi-turn conversation.
Every answer includes numbered citations from the retrieved chunks.

Usage:
    python rag_query.py                                      # interactive mode
    python rag_query.py --query "What are 2025's hottest AI verticals?"
    python rag_query.py --query "..." --top-k 8 --model gemini-2.5-flash
    python rag_query.py --query "..." --no-llm              # retrieval only (stub LLM)
"""
from __future__ import annotations

import argparse
import sys


def _make_parser() -> argparse.ArgumentParser:
    """Build the CLI parser. Kept separate so --help works without heavy deps."""
    parser = argparse.ArgumentParser(
        description="Query the startup-trends RAG system."
    )
    parser.add_argument("--query", "-q", help="Single query (non-interactive)")
    parser.add_argument(
        "--top-k", type=int, default=5,
        help="Chunks to retrieve (default: 5)",
    )
    parser.add_argument(
        "--model", default=None,
        help="LLM model string (default: LLM_MODEL from .env, e.g. ollama/llama3.1)",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM call, print retrieval results only",
    )
    return parser


# --- Early --help handling ----------------------------------------------------
# GitHub Classroom CI runs `python rag_query.py --help` in a minimal
# environment without our third-party dependencies installed.  Handle -h/--help
# BEFORE importing any heavy modules so the command succeeds anywhere.
if __name__ == "__main__" and ("-h" in sys.argv or "--help" in sys.argv):
    _make_parser().parse_args()   # prints help and sys.exit()s internally
    sys.exit(0)                   # safety net, shouldn't be reached

# --- Heavy imports (only reached when --help is NOT passed) -------------------
import os
from collections import defaultdict
from typing import Optional

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from config import (
    PG_DSN,
    EMBEDDING_MODEL,
    RAG_TOP_K,
    RAG_DEFAULT_MODEL,
    RAG_TEMPERATURE,
    RAG_MAX_HISTORY,
)
from llm import chat as llm_chat

console = Console()

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a research analyst specializing in early-stage startup investment "
    "trends from 2024 onwards. Your job is to answer questions based STRICTLY "
    "on the provided context excerpts from VC blog posts and industry reports.\n\n"
    "Rules:\n"
    "1. Base every claim on the provided context. If the context does not contain "
    "the answer, say so explicitly.\n"
    "2. When you make a claim, cite the source by its [#] number from the context.\n"
    "3. Be concise but specific. Prefer concrete numbers, company names, and dates "
    "over vague generalities.\n"
    "4. If sources contradict each other, acknowledge the disagreement."
)


def build_rag_prompt(question: str, chunks: list[dict]) -> str:
    context_blocks: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        src_label = chunk.get("title") or chunk.get("source_url") or "Unknown"
        url = chunk.get("source_url") or ""
        context_blocks.append(
            f"[{i}] Source: {src_label}"
            + (f" ({url})" if url else "")
            + f"\n{chunk['content']}\n"
        )
    context = "\n---\n".join(context_blocks)
    return (
        f"# Context\n{context}\n\n"
        f"# Question\n{question}\n\n"
        "# Answer (cite sources as [1], [2], etc.):\n"
    )


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

_embed_model: Optional[SentenceTransformer] = None


def get_embed_model() -> SentenceTransformer:
    global _embed_model
    if _embed_model is None:
        _embed_model = SentenceTransformer(EMBEDDING_MODEL)
    return _embed_model


def embed_query(text: str) -> np.ndarray:
    return get_embed_model().encode(text, normalize_embeddings=True, convert_to_numpy=True)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def get_connection() -> psycopg.Connection:
    conn = psycopg.connect(PG_DSN)
    register_vector(conn)
    return conn


def pgvector_search(
    conn: psycopg.Connection,
    query_vec: np.ndarray,
    top_k: int,
) -> list[dict]:
    """Return top-k most similar chunks with metadata."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                c.content,
                c.chunk_index,
                d.title,
                d.source_url,
                d.author,
                d.published_date,
                d.source_path,
                1 - (c.embedding <=> %s::vector) AS similarity
            FROM chunks c
            JOIN documents d ON c.document_id = d.id
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s;
            """,
            (query_vec.tolist(), query_vec.tolist(), top_k),
        )
        rows = cur.fetchall()

    return [
        {
            "content": row[0],
            "chunk_index": row[1],
            "title": row[2] or "",
            "source_url": row[3] or "",
            "author": row[4] or "",
            "published_date": str(row[5]) if row[5] else "",
            "source_path": row[6] or "",
            "similarity": float(row[7]),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def call_llm(
    messages: list[dict],
    model: str,
    stub: bool = False,
) -> str:
    """Generate an answer through llm.chat (any LiteLLM provider); stub returns a placeholder."""
    return llm_chat(messages, model=model, temperature=RAG_TEMPERATURE, stub=stub)


# ---------------------------------------------------------------------------
# Retrieval diversity
# ---------------------------------------------------------------------------

MAX_CHUNKS_PER_DOC: int = 2


def diversify_by_document(
    chunks: list[dict],
    top_k: int,
    max_per_doc: int = MAX_CHUNKS_PER_DOC,
) -> list[dict]:
    """Ensure no single document contributes more than max_per_doc chunks."""
    counts: dict[str, int] = defaultdict(int)
    result: list[dict] = []
    for c in chunks:  # already ordered by similarity desc
        key = c.get("title") or c.get("source_url") or "unknown"
        if counts[key] >= max_per_doc:
            continue
        counts[key] += 1
        result.append(c)
        if len(result) >= top_k:
            break
    return result


# ---------------------------------------------------------------------------
# Core query function (importable by skill_builder)
# ---------------------------------------------------------------------------

def query(
    user_question: str,
    conn: psycopg.Connection,
    top_k: int = RAG_TOP_K,
    model: str = RAG_DEFAULT_MODEL,
    history: Optional[list[dict]] = None,
    stub_llm: bool = False,
) -> dict:
    """
    Run one RAG turn.

    Returns:
        {
          "answer": str,
          "citations": [{"title", "url", "similarity"}, ...],
          "chunks": [raw chunk dicts],
        }
    """
    q_vec = embed_query(user_question)
    raw_chunks = pgvector_search(conn, q_vec, top_k=top_k * 3)  # over-fetch
    chunks = diversify_by_document(raw_chunks, top_k=top_k)      # per-doc cap

    rag_prompt = build_rag_prompt(user_question, chunks)

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": rag_prompt})

    answer = call_llm(messages, model=model, stub=stub_llm)

    citations = [
        {
            "title": c["title"],
            "url": c["source_url"],
            "similarity": round(c["similarity"], 4),
        }
        for c in chunks
    ]

    return {
        "answer": answer,
        "citations": citations,
        "chunks": chunks,
    }


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def print_answer(result: dict) -> None:
    console.print(Panel(Markdown(result["answer"]), title="Answer", border_style="green"))

    if result["citations"]:
        table = Table(title="Sources", show_header=True, header_style="bold cyan")
        table.add_column("#", width=3)
        table.add_column("Title")
        table.add_column("URL")
        table.add_column("Sim", justify="right", width=6)
        for i, c in enumerate(result["citations"], 1):
            table.add_row(
                str(i),
                c["title"][:60] if c["title"] else "—",
                c["url"][:60] if c["url"] else "—",
                f"{c['similarity']:.3f}",
            )
        console.print(table)


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

def interactive_loop(
    conn: psycopg.Connection,
    top_k: int,
    model: str,
    stub_llm: bool,
) -> None:
    console.print(
        Panel(
            "[bold]startup-trends-rag[/bold] interactive mode\n"
            "Type your question and press Enter. Type [bold]exit[/bold] to quit.",
            border_style="blue",
        )
    )

    history: list[dict] = []  # conversation memory

    while True:
        try:
            user_input = console.input("[bold cyan]> [/bold cyan]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Exiting.[/dim]")
            break

        if user_input.lower() in ("exit", "quit", "q"):
            break
        if not user_input:
            continue

        result = query(
            user_question=user_input,
            conn=conn,
            top_k=top_k,
            model=model,
            history=history if history else None,
            stub_llm=stub_llm,
        )

        print_answer(result)

        # Update history (keep last RAG_MAX_HISTORY turns)
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": result["answer"]})
        if len(history) > RAG_MAX_HISTORY * 2:
            history = history[-(RAG_MAX_HISTORY * 2):]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _make_parser()
    args = parser.parse_args()

    # Apply runtime defaults from config (we can't do this in _make_parser
    # because _make_parser must work without heavy deps)
    top_k = args.top_k if args.top_k else RAG_TOP_K
    model = args.model if args.model else RAG_DEFAULT_MODEL

    try:
        conn = get_connection()
    except Exception as exc:
        console.print(f"[red]Cannot connect to pgvector: {exc}[/red]")
        console.print("Is Docker running?  docker compose up -d")
        sys.exit(1)

    if args.query:
        result = query(
            user_question=args.query,
            conn=conn,
            top_k=top_k,
            model=model,
            stub_llm=args.no_llm,
        )
        print_answer(result)
    else:
        interactive_loop(conn, top_k=top_k, model=model, stub_llm=args.no_llm)

    conn.close()


if __name__ == "__main__":
    main()
