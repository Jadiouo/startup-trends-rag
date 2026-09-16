"""scripts/crawler.py — One-shot web crawler for startup-trends-rag.

Fetches articles from whitelisted VC blogs (RSS + Paul Graham HTML index),
filters to 2024+, and saves clean markdown with YAML frontmatter to
data/raw/crawled/.

Usage:
    python scripts/crawler.py                    # crawl all sources
    python scripts/crawler.py --source a16z      # only one source
    python scripts/crawler.py --dry-run          # list URLs, no fetch
    python scripts/crawler.py --force            # overwrite existing files
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
import feedparser
import trafilatura
from dateutil import parser as dateparser
from dateutil.tz import UTC
from tenacity import retry, stop_after_attempt, wait_exponential
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

# Allow running from project root or from scripts/ directly
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    CRAWLED_DIR,
    CRAWLER_CONCURRENCY,
    CRAWLER_DELAY,
    CRAWLER_TIMEOUT,
    CRAWLER_RETRY_ATTEMPTS,
    CRAWLER_MIN_DATE,
    POLITE_HEADERS,
    RSS_SOURCES,
    PG_INDEX_URL,
)

console = Console()
logging.basicConfig(
    filename="crawler_errors.log",
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
)

MIN_DATE = dateparser.parse(CRAWLER_MIN_DATE).replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def url_to_filename(source_name: str, url: str) -> str:
    """Convert a URL to a safe filename: <source>_<slug>.md"""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
    slug = slug[:80]  # max length
    return f"{source_name}_{slug}.md"


def already_exists(url: str) -> bool:
    """Check if any file in CRAWLED_DIR encodes this URL hash."""
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    for f in CRAWLED_DIR.glob("*.md"):
        if url_hash in f.read_text(encoding="utf-8", errors="ignore")[:500]:
            return True
    return False


def is_after_min_date(date_str: Optional[str]) -> bool:
    """Return True if date_str parses to >= MIN_DATE."""
    if not date_str:
        return False
    try:
        dt = dateparser.parse(date_str)
        if dt is None:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt >= MIN_DATE
    except Exception:
        return False


def build_frontmatter(
    source_url: str,
    title: str,
    author: str,
    published_date: str,
    source_name: str,
) -> str:
    crawled_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title_safe = title.replace('"', '\\"') if title else "Unknown"
    return (
        "---\n"
        f'source_url: "{source_url}"\n'
        f'title: "{title_safe}"\n'
        f'author: "{author or source_name}"\n'
        f'published_date: "{published_date or "unknown"}"\n'
        f'crawled_at: "{crawled_at}"\n'
        'license: "Public blog post, fair use for academic purposes"\n'
        "---\n\n"
    )


# ---------------------------------------------------------------------------
# HTTP / extraction
# ---------------------------------------------------------------------------

@retry(
    stop=stop_after_attempt(CRAWLER_RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def fetch(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url, headers=POLITE_HEADERS, timeout=CRAWLER_TIMEOUT)
    resp.raise_for_status()
    return resp.text


async def fetch_and_save(
    client: httpx.AsyncClient,
    url: str,
    source_name: str,
    force: bool,
    semaphore: asyncio.Semaphore,
    progress,
    task_id,
) -> bool:
    """Fetch one URL, extract text with trafilatura, save as .md. Returns True on success."""
    async with semaphore:
        filename = url_to_filename(source_name, url)
        out_path = CRAWLED_DIR / filename

        if out_path.exists() and not force:
            progress.advance(task_id)
            return False  # skip

        try:
            html = await fetch(client, url)
        except Exception as exc:
            logging.warning("fetch failed %s: %s", url, exc)
            progress.advance(task_id)
            return False

        # trafilatura extraction
        meta = trafilatura.extract_metadata(html)
        extracted = trafilatura.extract(
            html,
            output_format="markdown",
            with_metadata=False,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )

        if not extracted or len(extracted.strip()) < 200:
            logging.warning("extraction too short or failed: %s", url)
            progress.advance(task_id)
            return False

        # Date filtering
        pub_date = meta.date if meta else None
        if not is_after_min_date(pub_date):
            progress.advance(task_id)
            return False

        title = (meta.title or "").strip() if meta else ""
        author = (meta.author or "").strip() if meta else ""

        frontmatter = build_frontmatter(url, title, author, pub_date or "", source_name)
        # Embed URL hash in frontmatter comment for dedup
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        content = frontmatter + f"<!-- url_hash:{url_hash} -->\n\n" + extracted

        out_path.write_text(content, encoding="utf-8")
        await asyncio.sleep(CRAWLER_DELAY)
        progress.advance(task_id)
        return True


# ---------------------------------------------------------------------------
# Article discovery
# ---------------------------------------------------------------------------

async def discover_via_rss(client: httpx.AsyncClient, source: dict) -> list[str]:
    """Parse RSS feed (or JSON API) and return article URLs published after MIN_DATE."""
    # a16z uses WordPress JSON API
    if source.get("type") == "json_api":
        return await discover_via_json_api(client, source)

    try:
        html = await fetch(client, source["rss"])
    except Exception as exc:
        logging.warning("RSS fetch failed for %s: %s", source["name"], exc)
        return []

    feed = feedparser.parse(html)
    urls: list[str] = []
    for entry in feed.entries:
        pub = entry.get("published") or entry.get("updated") or ""
        if not is_after_min_date(pub):
            continue
        link = entry.get("link", "")
        if link:
            urls.append(link)
    return urls


async def discover_via_json_api(client: httpx.AsyncClient, source: dict) -> list[str]:
    """Discover articles via WordPress REST API (used by a16z)."""
    import json
    try:
        html = await fetch(client, source["rss"])
        posts = json.loads(html)
    except Exception as exc:
        logging.warning("JSON API fetch failed for %s: %s", source["name"], exc)
        return []

    urls: list[str] = []
    for post in posts:
        pub = post.get("date", "")
        if not is_after_min_date(pub):
            continue
        link = post.get("link", "")
        if link:
            urls.append(link)
    return urls


async def discover_paul_graham(client: httpx.AsyncClient) -> list[str]:
    """Parse paulgraham.com/articles.html HTML index."""
    try:
        html = await fetch(client, PG_INDEX_URL)
    except Exception as exc:
        logging.warning("PG index fetch failed: %s", exc)
        return []

    # Extract all /essays/<slug>.html links
    links = re.findall(r'href="(https?://paulgraham\.com/[^"]+\.html)"', html)
    if not links:
        # relative links
        links = re.findall(r'href="(/[^"]+\.html)"', html)
        links = [f"https://paulgraham.com{l}" for l in links]

    # PG doesn't reliably embed dates in the index; we take the first 10 to limit scope
    return list(dict.fromkeys(links))[:10]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(
    source_filter: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
) -> None:
    CRAWLED_DIR.mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(CRAWLER_CONCURRENCY)

    async with httpx.AsyncClient(http2=True, timeout=CRAWLER_TIMEOUT, follow_redirects=True, verify=False) as client:
        # Collect all (url, source_name) pairs
        all_articles: list[tuple[str, str]] = []

        sources_to_crawl = RSS_SOURCES
        if source_filter:
            sources_to_crawl = [s for s in RSS_SOURCES if s["name"] == source_filter]
            if not sources_to_crawl:
                console.print(f"[red]Unknown source: {source_filter}[/red]")
                console.print(f"Valid sources: {[s['name'] for s in RSS_SOURCES]}")
                return

        include_pg = source_filter is None or source_filter == "paulgraham"

        with Progress(
            SpinnerColumn("line"),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            disc_task = progress.add_task("Discovering articles...", total=None)

            for source in sources_to_crawl:
                urls = await discover_via_rss(client, source)
                for url in urls:
                    all_articles.append((url, source["name"]))
                progress.advance(disc_task)

            if include_pg:
                pg_urls = await discover_paul_graham(client)
                for url in pg_urls:
                    all_articles.append((url, "paulgraham"))
                progress.advance(disc_task)

        console.print(f"[bold]Discovered {len(all_articles)} articles[/bold]")

        if dry_run:
            for url, src in all_articles:
                console.print(f"  [{src}] {url}")
            return

        # Fetch & save
        with Progress(
            SpinnerColumn("line"),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            fetch_task = progress.add_task(
                "Fetching articles...", total=len(all_articles)
            )
            tasks = [
                fetch_and_save(client, url, src, force, semaphore, progress, fetch_task)
                for url, src in all_articles
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

    saved = sum(1 for r in results if r is True)
    skipped = sum(1 for r in results if r is False)
    errors = sum(1 for r in results if isinstance(r, Exception))

    console.print(
        f"\n[bold green]Done.[/bold green] "
        f"saved={saved}  skipped={skipped}  errors={errors}"
    )
    console.print(f"Output directory: {CRAWLED_DIR}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Crawl whitelisted VC blogs for 2024+ articles."
    )
    parser.add_argument("--source", help="Only crawl this source (e.g. a16z)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List discovered URLs without fetching",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch and overwrite existing files",
    )
    args = parser.parse_args()

    asyncio.run(main(source_filter=args.source, dry_run=args.dry_run, force=args.force))
