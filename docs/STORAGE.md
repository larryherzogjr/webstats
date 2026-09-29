# Storage architecture

Webstats uses SQLite in WAL mode. The workload has one periodic writer, two
local dashboard workers, and one administrator, so a separate database server
would add operations without improving the application model.

## Data lifetimes

- `requests` contains deduplicated parsed log records for the configured raw
  retention window. It is rebuildable from available nginx logs.
- `pages` records the permanent first and last successful human sightings for
  each site and path. New-page detection consults this registry instead of
  scanning every historical daily status row.
- `daily_*` tables contain permanent typed aggregates. Composite-key tables use
  SQLite `WITHOUT ROWID` storage on a fresh database.
- `events` contains permanent derived moments.
- `chronicle_*` contains public sitemap and robots observations.
- `rollup_days` is the lifecycle boundary. Open days may be rebuilt from raw
  rows. Sealed days have outlived raw retention and must never be replaced by
  partial late input. Late rows are discarded after being counted in
  `rollup_days.late_requests`.

`daily_filter` is the canonical daily site summary. Its four rows per site and
day preserve exact distinct-visitor counts for every combination of bot and
asset filtering. The former `daily_site` and `daily_traffic` tables duplicated
that information and have been removed.

## Ad Fontes collection boundary

`ad-fontes.app` participates in the same authenticated analytics as every
other configured site. Its privacy boundary is at collection: nginx logs
`$uri`, never `$request_uri`, and writes a literal `-` referrer. OAuth codes,
passage queries, and referrers therefore never enter the source log or SQLite
database. Raw IP addresses for every site are replaced in memory by daily
rotating hashes before insertion.

## Backups and recovery

Stop the dashboard and ingestion services before copying the database files.
Keep `webstats.db`, `webstats.db-wal`, `webstats.db-shm`, and
`ingest-state.json` together. A clean rebuild removes those active files,
initializes the current schema, and runs `scripts/backfill.py` against the
available live and rotated nginx logs.

The health endpoint reports open and sealed rollup-day counts plus discarded
late-row counts. A nonzero late count is diagnostic information: it means an
old log line arrived after that day's permanent history had already been
sealed.
