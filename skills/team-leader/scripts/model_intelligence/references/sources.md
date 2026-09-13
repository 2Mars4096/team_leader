# Source connectors

## Arena

- Dataset: `lmarena-ai/leaderboard-dataset`
- Discovery: `https://datasets-server.huggingface.co/splits?dataset=...`
- Rows: Hugging Face datasets-server `/first-rows`; oversized tables use the
  official Parquet file when PyArrow is available and paginated `/rows` otherwise.
- Use the `latest` split for current snapshots. The source also exposes historical splits.
- Arena scores are preference evidence. Preserve category, rank interval, vote count, style-control flag, and publish date when present.

## Artificial Analysis

- API: `https://artificialanalysis.ai/api/v2/language/models`
- Authentication: `x-api-key` from `ARTIFICIAL_ANALYSIS_API_KEY`
- The connector also supports the older free endpoint `/api/v2/data/llms/models` as a fallback.
- Attribution is required. Do not store or print the key.

## OpenRouter

- Catalog: `https://openrouter.ai/api/v1/models`
- Optional rankings: `https://openrouter.ai/api/v1/datasets/rankings-daily`
- `OPENROUTER_API_KEY` is optional for catalog metadata and required for authenticated datasets.
- Ranking traffic is adoption, not quality. Catalog records provide exact API slug, modalities, context, prices, and supported parameters.
- External worker calls use `POST /api/v1/chat/completions`. PDF inputs use the
  documented `file` content type and `file-parser` plugin.

## SWE-bench

- Leaderboard JSON: `https://raw.githubusercontent.com/SWE-bench/swe-bench.github.io/master/data/leaderboards.json`
- Treat each leaderboard variant separately. Scores are model-plus-agent-system results.

## Aider

- Polyglot results: `https://raw.githubusercontent.com/Aider-AI/aider/main/aider/website/_data/polyglot_leaderboard.yml`
- The file is a flat YAML sequence. The connector intentionally parses only its published scalar subset.
- Keep edit format, first/second pass rates, cost, time per case, and command model slug.

## LiveBench

- Repository: `LiveBench/new-livebench`
- Discover current `public/table_YYYY_MM_DD.csv` and matching category JSON through the GitHub Contents API.
- Store the release date from the filename. Objective benchmark categories should remain separate.

## Failure rules

A failed source must not invalidate successful sources. Record a failed snapshot with a sanitized error, leave the previous successful data intact, and return a nonzero exit code only when every requested source fails. Do not retry authentication failures. Use fixed retry counts for transient HTTP errors.
