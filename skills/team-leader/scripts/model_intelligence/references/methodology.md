# Methodology

The scripts provide a deterministic shortlist, not a universal model ranking.

## Identity

Every row retains the source model name and an exact variant key. A conservative family key removes provider prefixes, presentation punctuation, obvious date snapshots, and parenthetical effort labels. The family key is used only to combine evidence; the returned recommendation keeps the newest exact variant observed. Do not add an alias unless two names are known to represent the same model family. Never merge different sizes, `flash`/`pro`, vision/text variants, or different agent harnesses merely because their brand names resemble one another.

## Signal normalization

Within each source/benchmark/category snapshot, scores are converted to a 0–1 percentile. Published rank is preferred when both rank and cohort size are available. Otherwise numeric score order is used. A task profile averages matching categories within a signal, then combines signals using the fixed weights in `task-profiles.json`.

Coverage is disclosed and mildly penalized. A model supported by one benchmark should not outrank a similarly scoring model supported by several relevant independent signals without showing the evidence gap.

Cost and latency are reported but do not silently change quality ranking. Consumers may impose explicit price, provider, tool, or file-input requirements.

## Historical behavior

`--as-of` selects the latest successful snapshot from each source at or before the requested UTC date. The current date is never inferred from model release names. Raw snapshots are content-addressed and immutable; normalization can be rebuilt from them. The SQLite cache uses rollback journaling so a completed snapshot remains readable from read-only project and Dropbox mounts without creating WAL sidecar files.

## Context boundary

Only `query.py` output is suitable for direct prompt context. It includes the task profile, data date, shortlist, compact evidence, coverage, compatibility, and limitations. Raw payloads and database dumps are diagnostic artifacts and must remain outside model context.

## User evaluation

Public benchmarks are screening evidence. A permanent route should be validated on representative user tasks with the exact provider, model variant, reasoning setting, prompt, tool surface, and harness that will be deployed.
