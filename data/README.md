# data/

The corpus itself is **not** in the repository — the reports and blog posts
belong to their publishers. Everything here is reproducible from public URLs:

```
data/
├── raw/
│   ├── crawled/      <- scripts/crawler.py writes one .md per post (RSS whitelist in config.py)
│   └── manual/       <- PDFs / pages you download yourself; _sources.yaml lists what the
│       └── _sources.yaml   original corpus used (title, URL, author, date) and supplies metadata
└── processed/        <- plain text extracted by data_update.py (regenerated, ignored by git)
```

## Rebuilding the original corpus

1. `python scripts/crawler.py` — fetches 2024+ posts from the VC blogs whitelisted in
   `config.py` (`RSS_SOURCES`), respecting robots.txt and a 1 s per-domain delay.
2. Download the reports listed in `raw/manual/_sources.yaml` from their `source_url`
   and save them under `raw/manual/` with the exact file names used as keys.
3. `python data_update.py --rebuild`

## Using your own corpus

Drop any `.md` / `.txt` / `.pdf` files into `raw/manual/` (Markdown may carry
YAML front matter with `title`, `source_url`, `author`, `published_date`), add
them to `_sources.yaml` if you want metadata for files without front matter,
then run `python data_update.py`. Indexing is idempotent: unchanged files are
skipped, edited files are re-embedded, deleted files are removed from the DB.
