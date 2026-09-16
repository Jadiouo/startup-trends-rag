"""Retrieval-only evaluation: does the right document show up in the top-k?

For every question in eval/questions.yaml the query is embedded, pgvector
returns candidates, the per-document diversity cap is applied exactly as in
rag_query.py, and the question counts as a hit@k if any of its expected
substrings appears in a retrieved chunk's title / URL / source path.
Also reports MRR (1 / rank of the first hit) and lets you compare the raw
top-k against the diversified list with --no-diversity.

    python eval_retrieval.py                 # hit@5 with per-document cap
    python eval_retrieval.py --top-k 8
    python eval_retrieval.py --no-diversity  # plain pgvector top-k
    python eval_retrieval.py --verbose       # show what came back per question

Requires the pgvector container to be up and data_update.py to have run.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from rag_query import diversify_by_document, embed_query, get_connection, pgvector_search

console = Console()
EVAL_FILE = Path(__file__).resolve().parent / "eval" / "questions.yaml"


def source_fields(chunk: dict) -> str:
    return " | ".join(str(chunk.get(k, "")) for k in ("title", "source_url", "author", "source_path"))


def first_hit_rank(chunks: list[dict], expected: list[str]) -> int | None:
    for rank, ch in enumerate(chunks, start=1):
        haystack = source_fields(ch).lower()
        if any(e.lower() in haystack for e in expected):
            return rank
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--no-diversity", action="store_true", help="skip the per-document cap")
    ap.add_argument("--questions", default=str(EVAL_FILE))
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    items = yaml.safe_load(Path(args.questions).read_text(encoding="utf-8"))
    try:
        conn = get_connection()
    except Exception as exc:
        console.print(f"[red]Cannot connect to pgvector: {exc}[/red]  (docker compose up -d)")
        sys.exit(1)

    table = Table(title=f"Retrieval hit@{args.top_k}" + ("" if args.no_diversity else " (per-doc cap)"))
    table.add_column("#", justify="right")
    table.add_column("question")
    table.add_column("hit rank", justify="center")
    hits, rr_sum = 0, 0.0
    for i, item in enumerate(items, start=1):
        q_vec = embed_query(item["question"])
        if args.no_diversity:
            chunks = pgvector_search(conn, q_vec, top_k=args.top_k)
        else:
            chunks = diversify_by_document(pgvector_search(conn, q_vec, top_k=args.top_k * 3), top_k=args.top_k)
        rank = first_hit_rank(chunks, item["expect_any"])
        if rank:
            hits += 1
            rr_sum += 1.0 / rank
        table.add_row(str(i), item["question"][:70], f"[green]{rank}[/green]" if rank else "[red]miss[/red]")
        if args.verbose:
            for ch in chunks:
                console.print(f"    {ch['similarity']:.3f}  {ch['title'][:60]}  {ch['source_url'][:50]}")
    conn.close()

    console.print(table)
    n = len(items)
    console.print(f"hit@{args.top_k}: {hits}/{n} = {hits / n:.2f}    MRR: {rr_sum / n:.2f}")


if __name__ == "__main__":
    main()
