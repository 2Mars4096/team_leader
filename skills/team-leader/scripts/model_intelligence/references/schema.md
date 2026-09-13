# SQLite schema contract

The default database is `data/model_intelligence.sqlite3`.

## snapshots

One row per source refresh attempt. Successful snapshots reference a content-addressed raw file relative to the data directory. `fetched_at` and `source_date` use UTC ISO-8601 strings.

## observations

Normalized benchmark or catalog measurements. Important columns:

- `source`, `benchmark`, `category`
- `model_name`, `model_key`, `model_family`, `provider`
- `score`, `rank`, `rank_total`
- operational fields: prices, latency, throughput, context, tool/file support
- `reasoning_effort`, `harness`, `observed_at`
- `metadata_json` for source-specific fields that should not enter prompt context

Consumers must join through `snapshot_id` and use only successful snapshots. They must not assume that scores from different benchmarks share a scale.

## summaries

Precomputed compact JSON context packets keyed by task and snapshot fingerprint. These are caches; they can always be rebuilt from observations.

Schema migrations are explicit integer versions in `common.py`. Refuse a newer unknown schema instead of attempting a lossy downgrade.
