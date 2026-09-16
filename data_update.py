"""data_update.py — Idempotent indexing pipeline for startup-trends-rag.

Reads data/raw/ (markdown + PDF), extracts text, chunks, embeds locally,
and upserts into pgvector.  Fully reproducible: running it twice gives the
same result (SHA-256 content-hash diffing).

Usage:
    python data_update.py              # incremental update
    python data_update.py --rebuild    # full rebuild (wipe + re-index)
    python data_update.py --dry-run    # show diff without writing
    python data_update.py --verbose    # detailed logging
"""
from __future__ import annotations

import argparse
import sys


def _make_parser() -> argparse.ArgumentParser:
    """Build the CLI parser. Kept separate so --help works without heavy deps."""
    parser = argparse.ArgumentParser(description="Idempotent RAG indexing pipeline.")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Drop all documents and rebuild from scratch",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show diff without writing to DB",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose per-file logging",
    )
    return parser


# --- Early --help handling ----------------------------------------------------
# GitHub Classroom CI runs `python data_update.py --help` in a minimal
# environment without our third-party dependencies installed.  Handle -h/--help
# BEFORE importing any heavy modules so the command succeeds anywhere.
if __name__ == "__main__" and ("-h" in sys.argv or "--help" in sys.argv):
    _make_parser().parse_args()   # prints help and sys.exit()s internally
    sys.exit(0)                   # safety net, shouldn't be reached

# --- Heavy imports (only reached when --help is NOT passed) -------------------
import hashlib
import re
from pathlib import Path
from typing import Optional

import yaml
import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from pypdf import PdfReader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sentence_transformers import SentenceTransformer
from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from config import (
    RAW_DIR,
    MANUAL_DIR,
    PROCESSED_DIR,
    CHUNK_SIZE,
    CHUNK_OVERLAP,
    CHUNK_SEPARATORS,
    EMBEDDING_MODEL,
    EMBEDDING_DIM,
    PG_DSN,
)

console = Console()

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_connection() -> psycopg.Connection:
    conn = psycopg.connect(PG_DSN)
    register_vector(conn)
    return conn


def ensure_schema(conn: psycopg.Connection) -> None:
    """Create tables / indexes if they don't exist yet."""
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id              SERIAL PRIMARY KEY,
                source_path     TEXT NOT NULL UNIQUE,
                source_url      TEXT,
                source_type     TEXT NOT NULL,
                title           TEXT,
                author          TEXT,
                published_date  DATE,
                content_hash    TEXT NOT NULL,
                char_count      INTEGER,
                ingested_at     TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);"
        )
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS chunks (
                id              SERIAL PRIMARY KEY,
                document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                chunk_index     INTEGER NOT NULL,
                content         TEXT NOT NULL,
                embedding       vector({EMBEDDING_DIM}) NOT NULL,
                token_count     INTEGER,
                UNIQUE(document_id, chunk_index)
            );
        """)
        # HNSW index — only create if it doesn't exist
        cur.execute("""
            SELECT 1 FROM pg_indexes
            WHERE tablename = 'chunks' AND indexname = 'idx_chunks_embedding';
        """)
        if cur.fetchone() is None:
            cur.execute("""
                CREATE INDEX idx_chunks_embedding
                ON chunks USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64);
            """)
    conn.commit()


def fetch_db_documents(conn: psycopg.Connection) -> dict[str, str]:
    """Return {source_path: content_hash} for all documents in DB."""
    with conn.cursor() as cur:
        cur.execute("SELECT source_path, content_hash FROM documents;")
        return {row[0]: row[1] for row in cur.fetchall()}


def delete_document(conn: psycopg.Connection, source_path: str) -> None:
    """Delete document and cascade-delete its chunks."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM documents WHERE source_path = %s;", (source_path,))
    conn.commit()


def insert_document(
    conn: psycopg.Connection,
    source_path: str,
    source_url: str,
    source_type: str,
    title: str,
    author: str,
    published_date: Optional[str],
    content_hash: str,
    char_count: int,
) -> int:
    """Insert document row, return its id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents
                (source_path, source_url, source_type, title, author,
                 published_date, content_hash, char_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
            """,
            (
                source_path,
                source_url,
                source_type,
                title,
                author,
                published_date or None,
                content_hash,
                char_count,
            ),
        )
        doc_id: int = cur.fetchone()[0]  # type: ignore[index]
    conn.commit()
    return doc_id


def insert_chunks(
    conn: psycopg.Connection,
    doc_id: int,
    texts: list[str],
    embeddings: np.ndarray,
) -> None:
    with conn.cursor() as cur:
        for idx, (text, vec) in enumerate(zip(texts, embeddings)):
            cur.execute(
                """
                INSERT INTO chunks (document_id, chunk_index, content, embedding, token_count)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (document_id, chunk_index) DO UPDATE
                    SET content = EXCLUDED.content,
                        embedding = EXCLUDED.embedding,
                        token_count = EXCLUDED.token_count;
                """,
                (doc_id, idx, text, vec.tolist(), len(text.split())),
            )
    conn.commit()


# ---------------------------------------------------------------------------
# File I/O & text extraction
# ---------------------------------------------------------------------------

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Strip YAML frontmatter, return (meta_dict, body_text)."""
    meta: dict[str, str] = {}
    if not text.startswith("---"):
        return meta, text
    end = text.find("---", 3)
    if end == -1:
        return meta, text
    fm_block = text[3:end].strip()
    body = text[end + 3:].strip()
    for line in fm_block.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip().strip('"')
    return meta, body


def extract_text_md(path: Path) -> tuple[dict[str, str], str]:
    """Read markdown file, return (frontmatter_dict, clean_body)."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    meta, body = parse_frontmatter(raw)
    # Remove HTML comments (url_hash markers inserted by crawler)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).strip()
    return meta, body


def extract_text_pdf(path: Path) -> tuple[dict[str, str], str]:
    """Extract text from PDF, return (empty meta, text)."""
    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        pages.append(text)
    body = "\n\n".join(pages)
    return {}, body


def clean_text(text: str) -> str:
    """Remove excessive whitespace and common PDF artifacts."""
    # Strip NUL bytes (PostgreSQL text fields reject them)
    text = text.replace("\x00", "")
    # Collapse 3+ blank lines → 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Remove page-number-only lines (e.g. "  12  " or "Page 12")
    text = re.sub(r"(?m)^\s*(Page\s+)?\d+\s*$", "", text)
    text = text.strip()
    return text


def load_manual_metadata() -> dict[str, dict[str, str]]:
    """Load _sources.yaml from data/raw/manual/ for PDF/manual file metadata."""
    yaml_path = MANUAL_DIR / "_sources.yaml"
    if not yaml_path.exists():
        return {}
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def save_processed(source_path: str, text: str) -> Path:
    """Save clean text to data/processed/<stem>.txt."""
    stem = Path(source_path).stem
    out = PROCESSED_DIR / f"{stem}.txt"
    out.write_text(text, encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Chunking & embedding
# ---------------------------------------------------------------------------

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    separators=CHUNK_SEPARATORS,
    length_function=len,
)


def chunk_text(text: str) -> list[str]:
    return _splitter.split_text(text)


_embed_model: Optional[SentenceTransformer] = None


def get_embed_model() -> SentenceTransformer:
    global _embed_model
    if _embed_model is None:
        console.print(f"[dim]Loading embedding model {EMBEDDING_MODEL}…[/dim]")
        _embed_model = SentenceTransformer(EMBEDDING_MODEL)
    return _embed_model


def embed_chunks(texts: list[str]) -> np.ndarray:
    model = get_embed_model()
    return model.encode(
        texts,
        batch_size=32,
        show_progress_bar=False,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )


# ---------------------------------------------------------------------------
# Process one file
# ---------------------------------------------------------------------------

_manual_meta: dict[str, dict[str, str]] | None = None


def _get_manual_meta() -> dict[str, dict[str, str]]:
    global _manual_meta
    if _manual_meta is None:
        _manual_meta = load_manual_metadata()
    return _manual_meta


def process_file(
    path: Path,
    conn: psycopg.Connection,
    verbose: bool = False,
) -> None:
    source_path = str(path.relative_to(Path.cwd()).as_posix()) if path.is_relative_to(Path.cwd()) else str(path)

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        meta, body = extract_text_pdf(path)
        source_type = "pdf"
    else:  # .md, .txt
        meta, body = extract_text_md(path)
        source_type = "markdown"

    # Overlay metadata from _sources.yaml (for manual files without frontmatter)
    manual_meta = _get_manual_meta()
    yaml_meta = manual_meta.get(path.name, {})
    for key in ("title", "source_url", "author", "published_date"):
        if yaml_meta.get(key) and not meta.get(key):
            meta[key] = yaml_meta[key]

    body = clean_text(body)
    if not body:
        if verbose:
            console.print(f"  [yellow]skip (empty body):[/yellow] {path.name}")
        return

    save_processed(source_path, body)

    chunks = chunk_text(body)
    if not chunks:
        return

    embeddings = embed_chunks(chunks)

    content_hash = sha256_of(path)
    doc_id = insert_document(
        conn=conn,
        source_path=source_path,
        source_url=meta.get("source_url", ""),
        source_type=source_type,
        title=meta.get("title", path.stem),
        author=meta.get("author", ""),
        published_date=meta.get("published_date", "") or None,
        content_hash=content_hash,
        char_count=len(body),
    )
    insert_chunks(conn, doc_id, chunks, embeddings)

    if verbose:
        console.print(
            f"  [green]indexed[/green] {path.name} "
            f"({len(chunks)} chunks, {len(body):,} chars)"
        )


# ---------------------------------------------------------------------------
# Sync algorithm (idempotency)
# ---------------------------------------------------------------------------

def walk_raw() -> list[Path]:
    """Return all .md and .pdf files under data/raw/ (exclude _sources.yaml)."""
    files: list[Path] = []
    for ext in ("*.md", "*.pdf"):
        files.extend(p for p in RAW_DIR.rglob(ext) if not p.name.startswith("_"))
    return files


def normalize_source_path(path: Path) -> str:
    """Normalize path the same way process_file() stores it in DB."""
    cwd = Path.cwd()
    if path.is_relative_to(cwd):
        return str(path.relative_to(cwd).as_posix())
    return str(path)


def sync(
    conn: psycopg.Connection,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    raw_files = walk_raw()

    # Build {source_path: sha256} using the same path format as process_file()
    fs_map: dict[str, str] = {}
    path_lookup: dict[str, Path] = {}  # normalized_path → original Path
    for p in raw_files:
        sp = normalize_source_path(p)
        fs_map[sp] = sha256_of(p)
        path_lookup[sp] = p

    db_map = fetch_db_documents(conn)

    new_files = set(fs_map.keys()) - set(db_map.keys())
    deleted_files = set(db_map.keys()) - set(fs_map.keys())
    changed_files = {
        p for p in (set(fs_map.keys()) & set(db_map.keys()))
        if fs_map[p] != db_map[p]
    }
    unchanged_count = len(fs_map) - len(new_files) - len(changed_files)

    # Summary table
    table = Table(title="Data Sync Status", show_header=True)
    table.add_column("Category", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("[green]new[/green]", str(len(new_files)))
    table.add_row("[yellow]changed[/yellow]", str(len(changed_files)))
    table.add_row("[red]deleted[/red]", str(len(deleted_files)))
    table.add_row("[dim]unchanged[/dim]", str(unchanged_count))
    console.print(table)

    if dry_run:
        if new_files:
            console.print("[green]New:[/green]")
            for p in sorted(new_files):
                console.print(f"  + {p}")
        if changed_files:
            console.print("[yellow]Changed:[/yellow]")
            for p in sorted(changed_files):
                console.print(f"  ~ {p}")
        if deleted_files:
            console.print("[red]Deleted:[/red]")
            for p in sorted(deleted_files):
                console.print(f"  - {p}")
        return

    to_process = new_files | changed_files

    with Progress(
        SpinnerColumn("line"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        # Delete changed (cascade removes chunks)
        if changed_files:
            del_task = progress.add_task("Deleting changed docs…", total=len(changed_files))
            for sp in changed_files:
                delete_document(conn, sp)
                progress.advance(del_task)

        # Delete removed
        if deleted_files:
            del_task2 = progress.add_task("Deleting removed docs…", total=len(deleted_files))
            for sp in deleted_files:
                delete_document(conn, sp)
                progress.advance(del_task2)

        # Index new + changed
        if to_process:
            idx_task = progress.add_task("Indexing files…", total=len(to_process))
            for sp in sorted(to_process):
                process_file(path_lookup[sp], conn, verbose=verbose)
                progress.advance(idx_task)

    console.print(
        f"\n[bold green]Sync complete.[/bold green] "
        f"Indexed {len(to_process)} file(s), deleted {len(deleted_files)} orphan(s)."
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    args = _make_parser().parse_args()

    console.print("[bold]startup-trends-rag — data_update[/bold]")

    try:
        conn = get_connection()
    except Exception as exc:
        console.print(f"[red]Cannot connect to pgvector: {exc}[/red]")
        console.print("Is Docker running?  docker compose up -d")
        sys.exit(1)

    ensure_schema(conn)

    if args.rebuild and not args.dry_run:
        console.print("[yellow]--rebuild: dropping all documents…[/yellow]")
        with conn.cursor() as cur:
            cur.execute("DELETE FROM documents;")
        conn.commit()
        console.print("[green]All documents cleared.[/green]")

    sync(conn, dry_run=args.dry_run, verbose=args.verbose)
    conn.close()


if __name__ == "__main__":
    main()
