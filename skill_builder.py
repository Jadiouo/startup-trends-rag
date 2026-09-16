"""skill_builder.py — Knowledge distillation for startup-trends-rag.

Runs 13 global questions against the RAG system, then uses a synthesis
prompt to produce a structured skill.md document.

Usage:
    python skill_builder.py --output skill.md
    python skill_builder.py --output skill.md --top-k 8 --model gemini-2.5-flash
    python skill_builder.py --dry-run          # run RAG but skip synthesis
"""
from __future__ import annotations

import argparse
import sys


def _make_parser() -> argparse.ArgumentParser:
    """Build the CLI parser. Kept separate so --help works without heavy deps."""
    parser = argparse.ArgumentParser(
        description="Build skill.md via knowledge distillation over the RAG system."
    )
    parser.add_argument(
        "--output", "-o", default="skill.md", help="Output path (default: skill.md)"
    )
    parser.add_argument(
        "--top-k", type=int, default=8, help="Chunks to retrieve per question (default: 8)"
    )
    parser.add_argument(
        "--model", default=None, help="LLM model string (default: LLM_MODEL from .env)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run RAG questions but skip synthesis and file write",
    )
    return parser


# --- Early --help handling ----------------------------------------------------
if __name__ == "__main__" and ("-h" in sys.argv or "--help" in sys.argv):
    _make_parser().parse_args()
    sys.exit(0)

# --- Heavy imports (only reached when --help is NOT passed) -------------------
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg
from pgvector.psycopg import register_vector
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.panel import Panel

from config import PG_DSN, RAG_DEFAULT_MODEL
from rag_query import query as rag_query

console = Console()

# ---------------------------------------------------------------------------
# Global questions (maps to skill.md sections)
# ---------------------------------------------------------------------------

GLOBAL_QUESTIONS: list[dict] = [
    # → Overview
    {
        "section": "overview",
        "question": (
            "Summarize the overall landscape of early-stage startup investment "
            "from 2024 to 2026 in 3-4 sentences. What are the dominant themes?"
        ),
    },
    # → Core Concepts
    {
        "section": "core_concepts",
        "question": (
            "What specific evaluation frameworks, mental models, or investment "
            "heuristics do leading VCs like a16z, Sequoia, Bessemer, or Y Combinator "
            "alumni articulate in their writings about early-stage startup investing "
            "in 2024-2026?"
        ),
    },
    {
        "section": "core_concepts",
        "question": (
            "What new business model patterns or go-to-market strategies have "
            "emerged for early-stage startups in this period?"
        ),
    },
    {
        "section": "core_concepts",
        "question": (
            "How has the definition of 'product-market fit' or 'traction' evolved "
            "for AI-native startups specifically?"
        ),
    },
    {
        "section": "core_concepts",
        "question": (
            "What mental models do founders like Sam Altman, Paul Graham, or "
            "Patrick Collison recommend for thinking about startup success and "
            "scaling in the current environment?"
        ),
    },
    # → Key Trends
    {
        "section": "key_trends",
        "question": (
            "What are the top investment trends and hottest sectors in early-stage "
            "venture capital from 2024 to 2026? List with specific examples and data "
            "where possible."
        ),
    },
    {
        "section": "key_trends",
        "question": (
            "How has the AI boom specifically reshaped early-stage startup investment "
            "patterns? Which AI sub-sectors are attracting the most capital?"
        ),
    },
    {
        "section": "key_trends",
        "question": (
            "What contrarian or non-consensus investment theses have leading VCs "
            "articulated for 2024-2026?"
        ),
    },
    # → Key Entities
    {
        "section": "key_entities",
        "question": (
            "Who are the most influential VC firms, partners, and thought leaders "
            "shaping the early-stage investment narrative in 2024-2026? What are "
            "they known for?"
        ),
    },
    # → Methodology & Best Practices
    {
        "section": "methodology",
        "question": (
            "What best practices or methodologies do experienced founders and VCs "
            "(such as Sam Altman, Paul Graham, a16z, Sequoia, Index Ventures, "
            "Bessemer) recommend for early-stage company building, fundraising, "
            "and scaling in the current environment?"
        ),
    },
    # → Knowledge Gaps
    {
        "section": "gaps",
        "question": (
            "Looking across all the indexed sources, what important topics about "
            "early-stage startup investment remain under-discussed or absent? "
            "Consider what a complete knowledge base should cover but this corpus "
            "does not."
        ),
    },
    # → Example Q&A seed
    {
        "section": "example_qa",
        "question": (
            "What are the biggest risks facing early-stage AI startups in 2025-2026?"
        ),
    },
    {
        "section": "example_qa",
        "question": (
            "How should a first-time founder think about choosing between bootstrapping "
            "and raising venture capital today?"
        ),
    },
]

# ---------------------------------------------------------------------------
# skill.md template
# ---------------------------------------------------------------------------

SKILL_TEMPLATE = """\
# Early-Stage Startup Investment Trends (2024–2026)

> This skill file was auto-generated by `skill_builder.py` using a RAG system
> over curated VC blog posts and industry reports.
> **Last generated**: {generated_at}

---

## Overview

{overview}

---

## Core Concepts

{core_concepts}

---

## Key Trends

{key_trends}

---

## Key Entities

{key_entities}

---

## Methodology & Best Practices

{methodology}

---

## Knowledge Gaps & Open Questions

{gaps}

---

## Example Q&A

{example_qa}

---

## Source References

{source_references}
"""

# ---------------------------------------------------------------------------
# Synthesis prompt
# ---------------------------------------------------------------------------

SYNTHESIS_PROMPT = """\
You are an expert technical writer creating a knowledge distillation document \
called a "skill file" for an AI agent. Below are 13 questions and their answers, \
generated by a RAG system over a curated corpus of VC blog posts and industry \
reports about early-stage startup investment (2024-2026).

Your task: synthesize these into a clean, well-structured `skill.md` document \
following the EXACT template provided. Be concise but information-dense. \
Preserve specific facts, names, numbers, and citations from the source answers. \
Do NOT add information that wasn't in the source answers.

# Template
{template}

# Source Q&A
{qa_dump}

# Output
Return ONLY the markdown content, no preamble.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_connection() -> psycopg.Connection:
    conn = psycopg.connect(PG_DSN)
    register_vector(conn)
    return conn


def fetch_all_sources(conn: psycopg.Connection) -> list[dict]:
    """Pull all documents from DB for the Source References section."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT title, source_url, author, published_date FROM documents ORDER BY published_date DESC NULLS LAST;"
        )
        return [
            {
                "title": row[0] or "Unknown",
                "url": row[1] or "",
                "author": row[2] or "",
                "date": str(row[3]) if row[3] else "",
            }
            for row in cur.fetchall()
        ]


def format_source_references(sources: list[dict]) -> str:
    if not sources:
        return "_No indexed documents found._"
    lines: list[str] = []
    for src in sources:
        title = src["title"]
        url = src["url"]
        author = src["author"]
        date = src["date"]
        line = f"- **{title}**"
        if author:
            line += f" — {author}"
        if date:
            line += f" ({date})"
        if url:
            line += f"  \n  {url}"
        lines.append(line)
    return "\n".join(lines)


def build_qa_dump(qa_results: list[dict]) -> str:
    blocks: list[str] = []
    for item in qa_results:
        blocks.append(
            f"## Q ({item['section']})\n{item['question']}\n\n"
            f"## A\n{item['answer']}\n"
        )
    return "\n\n---\n\n".join(blocks)


def call_synthesis_llm(prompt: str, model: str, stub: bool) -> str:
    """Synthesize the final skill.md content (any LiteLLM provider via llm.chat)."""
    if stub:
        return "[synthesis skipped — dry-run mode]"
    from llm import chat

    return chat([{"role": "user", "content": prompt}], model=model, temperature=0.2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_skill(
    output_path: Path,
    top_k: int = 8,
    model: str = RAG_DEFAULT_MODEL,
    dry_run: bool = False,
) -> None:
    console.print("[bold]startup-trends-rag — skill_builder[/bold]")

    try:
        conn = get_connection()
    except Exception as exc:
        console.print(f"[red]Cannot connect to pgvector: {exc}[/red]")
        console.print("Is Docker running?  docker compose up -d")
        sys.exit(1)

    # --- Step 1: Run all global questions through RAG ---
    qa_results: list[dict] = []

    with Progress(
        SpinnerColumn("line"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("Running global questions…", total=len(GLOBAL_QUESTIONS))

        for item in GLOBAL_QUESTIONS:
            result = rag_query(
                user_question=item["question"],
                conn=conn,
                top_k=top_k,
                model=model,
                stub_llm=dry_run,
            )
            qa_results.append(
                {
                    "section": item["section"],
                    "question": item["question"],
                    "answer": result["answer"],
                    "citations": result["citations"],
                }
            )
            progress.advance(task_id)

    console.print(f"[green]Collected {len(qa_results)} Q&A pairs.[/green]")

    if dry_run:
        console.print("[yellow]--dry-run: skipping synthesis and file write.[/yellow]")
        for item in qa_results:
            console.print(
                Panel(
                    f"[bold]{item['question']}[/bold]\n\n{item['answer']}",
                    title=f"[{item['section']}]",
                    border_style="dim",
                )
            )
        conn.close()
        return

    # --- Step 2: Collect source references from DB ---
    sources = fetch_all_sources(conn)
    source_refs = format_source_references(sources)

    # --- Step 3: Synthesis ---
    console.print("[dim]Synthesizing skill.md…[/dim]")

    qa_dump = build_qa_dump(qa_results)
    template_filled = SKILL_TEMPLATE.format(
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        overview="[see synthesis]",
        core_concepts="[see synthesis]",
        key_trends="[see synthesis]",
        key_entities="[see synthesis]",
        methodology="[see synthesis]",
        gaps="[see synthesis]",
        example_qa="[see synthesis]",
        source_references=source_refs,
    )

    synthesis_prompt = SYNTHESIS_PROMPT.format(
        template=template_filled,
        qa_dump=qa_dump,
    )

    synthesized_md = call_synthesis_llm(synthesis_prompt, model=model, stub=dry_run)

    # If synthesis worked, append the source references (since LLM might omit it)
    if "[synthesis" not in synthesized_md and "## Source References" not in synthesized_md:
        synthesized_md += f"\n\n---\n\n## Source References\n\n{source_refs}\n"

    # --- Step 4: Write output ---
    output_path.write_text(synthesized_md, encoding="utf-8")
    console.print(
        f"\n[bold green]skill.md written:[/bold green] {output_path} "
        f"({len(synthesized_md):,} chars)"
    )

    conn.close()


def main() -> None:
    parser = _make_parser()
    args = parser.parse_args()

    model = args.model if args.model else RAG_DEFAULT_MODEL

    build_skill(
        output_path=Path(args.output),
        top_k=args.top_k,
        model=model,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
