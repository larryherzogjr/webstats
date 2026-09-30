# Webstats

Webstats is a private, self-hosted traffic dashboard for nginx access logs. It
uses no browser tracking, sends no analytics to third parties, and never writes
raw IP addresses to disk. A Python ingest process tails each configured log,
stores short-lived request rows in SQLite, and retains daily aggregates. A Flask
application presents overview, site-detail, live, and health pages.

The project is designed for one administrator and one Linux server. Site names,
log paths, retention, timezone, and log format all live in TOML configuration.

## What is included

- Incremental binary-safe log tailing with inode and byte-offset state.
- Recovery across standard nginx rotation to an uncompressed `.1` file.
- Gzip history backfill and source-level duplicate protection.
- Standard combined, host-prefixed combined, and explicit nginx format parsing.
- Daily rotating IP hashes for daily visitor counts without cross-day tracking.
- Bot, browser, operating-system, static-asset, referrer, status, and optional
  country classification.
- Automatic Moments for newly discovered content, first-time referrers, AI
  crawler sightings, traffic records and spikes, visitor-day milestones, and
  explicitly reported RSS subscriber milestones. Gold chart markers connect
  each observation to the traffic around it.
- A dashboard-native Discovery Inbox with browser-local unread state, automatic
  discovery/reader/momentum grouping, site and date scopes, and direct links to
  the relevant page story or intelligence view. No external notification
  service or manual annotation is required.
- A ten-second Live Radar with a rolling human activity stream, Automatic
  Moment badges, relative timestamps, and a self-contained world pulse map.
- An RSS readership view that recognizes hosted and self-hosted feed clients,
  charts explicitly reported subscription totals, and keeps unreported readers
  visible without inventing subscriber counts from fetch frequency.
- A Traffic Almanac with site and server-wide year heatmaps, all-time records,
  active-day streaks, record-breaking days, and visitor-day milestones.
- Automatic Weekly Briefings that compare like-for-like periods, explain the
  largest changes in plain language, collect discoveries and unusual moments,
  and provide a migration-free archive generated from permanent rollups.
- A Change Engine for any selected date range that compares the immediately
  preceding equal-length window and attributes movement to pages, referrers,
  countries, bots, AI crawlers, feed readers, and errors, with direct links to
  the relevant drill-downs.
- Attention Episodes that detect statistically unusual human traffic, join
  related surge days, reconstruct the rise, peak, decay, and following week,
  and attribute the episode to pages, referrers, countries, recognized
  crawlers, errors, and Automatic Moments without manual annotations.
- Performance & Reliability intelligence built from nginx request timing,
  including slow pages, equal-window latency regressions, response-size
  anomalies, application-error bursts and recovery, and a separate accounting
  of recognized scanner noise.
- A Content Observatory that discovers same-site HTTPS sitemaps automatically,
  identifies never-observed, crawler-only, quiet, and crawler-heavy pages,
  measures search and AI coverage, finds active pages outside the published
  inventory, and surfaces current robots/sitemap policy collisions.
- A Living Web Chronicle that keeps a compact, searchable history of public
  sitemap and robots-policy changes, detects publishing, updates, removals,
  redirects, disappearances, and returns, and connects each page change with
  later search and AI crawler reactions.
- Content Pulse classifications for debuts, rising and cooling pages,
  evergreen content, dormant pages, and content resurfacing after a long quiet
  spell, with the referrer, country, and AI activity behind each signal.
- Error Intelligence that separates known probe noise from human 404s, flags
  regressions and persistent misses, and suggests likely intended paths for
  typo-shaped requests without changing the underlying traffic history.
- Reading Paths that infer anonymous 30-minute visits, entrances, exits,
  visit depth, and page-to-page transitions. Consecutive refreshes collapse,
  and only daily aggregates persist.
- Link Atlas for the host-level relationships between referring sites and
  destination pages, including new, rising, loyal, resurfaced, cooling, and
  dormant sources, equal-period comparisons, and source drill-downs. Full
  referring URLs and query strings are never retained.
- Page Galaxy, a dependency-free SVG constellation joining the busiest pages
  to referring hosts, anonymous aggregate Reading Paths, and recognized AI
  readers. Layers can be hidden independently and every node opens its source
  intelligence or page story.
- An AI Policy Observatory that fetches each configured site's current public
  `robots.txt`, applies explicit and wildcard AI-agent rules, and compares them
  with observed crawler paths. Policy conflicts are presented as investigation
  signals rather than claims about intent or past policy.
- Clickable per-page stories with permanent daily history, lifetime first and
  last sightings, referrers, AI readers, countries, and response codes.
- Idempotent daily rollups retained after raw request pruning, with an explicit
  open/sealed-day ledger that prevents late partial logs from replacing
  complete history.
- Authenticated JSON APIs and responsive server-rendered pages.
- Bookmarkable all-site or single-site scopes across Live Radar, Weekly
  Briefings, the Change Engine, Content Observatory, AI Crawlers, Feeds,
  Reading Paths, Link Atlas, and the Traffic Almanac.
- Zero-filled daily charts, automatic hourly charts for retained one-day raw
  data, daily fallback for older dates, and bookmarkable date and traffic
  filters.
- Local Chart.js 4.4.7 bundle with no CDN or front-end build step.
- Bcrypt login, secure session cookies, login throttling, and public safe health
  status.
- systemd, nginx, health-check, password, and backfill assets.
- Versioned SQLite migrations and GitHub Actions checks for Python 3.11 and 3.12.

Automatic Moments, the AI field guide, and the RSS readership view use
permanent daily aggregates. They add no persistent visitor identifier and do
not extend raw request or IP-hash retention. New-page moments require a
successful human, non-asset request, ignore common probe paths, and preserve
only the first sighting. Aggregate moments are rebuilt deterministically, so
ingest and backfill reruns cannot duplicate them.

The second-generation SQLite layout separates temporary request facts from
permanent analytics. `pages` is the canonical first/last-sighting registry,
`daily_filter` is the canonical site summary for the four bot/asset scopes, and
the other daily tables retain only their typed dimensions. Composite-key
aggregate tables use `WITHOUT ROWID`. `rollup_days` records whether each day is
open or sealed; a sealed day is never rebuilt from late partial raw input.
`daily_site` and `daily_traffic` were removed because they duplicated
`daily_filter`. See [`docs/STORAGE.md`](docs/STORAGE.md) for the lifecycle,
privacy-at-collection, backup, and recovery model.

RSS subscriber totals are deliberately conservative. Inoreader and some other
services include an explicit subscriber count in their fetcher user agent;
Webstats records that reported number. Readers such as current Feedly identify
their fetcher without promising a count, so Webstats shows the reader and its
feed requests but labels the subscriber total as unreported. A shared fetch is
not treated as one person, and repeated fetches are never used as a proxy for
subscriber growth. Subscriber reports are deduplicated by site and reader even
when the service polls multiple feed paths; reports older than 14 days are not
included in the current total.

Link Atlas and the Change Engine use permanent daily aggregates, so they
continue to work after raw-log retention expires without adding a migration or
a visitor identifier. `ad-fontes.app` participates normally in authenticated
analytics, but its dedicated nginx format never records query strings or
referrers. OAuth codes and passage queries therefore never enter Webstats.

Attention Episodes use a 28-day adaptive baseline. A trigger must reach at
least five human page requests, twice its preceding average, two population
standard deviations above that average, and three requests above baseline.
Triggers no more than two days apart are joined, decay is followed for up to
seven days, and the following week is classified as returned, sustained, or
not yet resolved. Attribution is descriptive rather than causal. The feature
uses existing permanent daily rollups.

Performance timing uses nginx's `$request_time`, converted to milliseconds at
ingest. The built-in `combined_host` parser accepts both the new timed suffix
and legacy untimed lines, so current and rotated logs can coexist safely.
Successful human, non-asset GET requests feed permanent daily latency and
response-size summaries. Latency naturally begins after the timed nginx format
is installed; migration rebuilds response-size and error history from retained
raw rows. Multi-day p95 displays are explicitly labelled as sample-weighted
daily percentiles rather than an exact percentile across the whole range.

The AI Policy Observatory caches current public `robots.txt` responses in
application memory for 15 minutes and compares those current rules with the
selected historical traffic window. Its conflict label
therefore means “this observed path is disallowed now,” not necessarily that
the same rule existed when the request occurred. The separate Living Web
Chronicle archives each distinct observed policy state for future comparisons.

The Content Observatory follows sitemap declarations in `robots.txt` and falls
back to the conventional `/sitemap.xml`, `/sitemap_index.xml`, and
`/sitemap-index.xml` locations. Discovery accepts only same-site HTTPS URLs,
follows only same-site HTTPS redirects, ignores static-asset entries, and is
capped at eight seconds, 24 sitemap documents, and 10,000 page URLs per site.
Current sitemap inventories are held in application memory for 30 minutes by
the interactive Observatory. The scheduled Chronicle stores compact inventory
state and changes, not complete sitemap documents or new visitor identifiers.

The Chronicle observer runs twice daily by default. It accepts only bounded,
same-site HTTPS sitemap discovery. When a page leaves an available sitemap, it
probes at most 64 removed URLs per site per run without following redirects, so
it can distinguish a same-site redirect, HTTP 404/410 disappearance, and an
unlisted page that remains reachable. An unavailable, partial, capped, or
otherwise errored sitemap never causes a mass-removal event. Probe network
failures preserve the previous page state, and all origin requests finish
before Chronicle takes SQLite's writer lock. Public inventory changes are
retained for all configured sites.

## Fresh installation in 14 steps

These commands target Debian or Ubuntu. Perform the discovery checks in
[`docs/DISCOVERY.md`](docs/DISCOVERY.md) first. Do not enable ingestion until the
actual access-log paths and format are confirmed.

1. Install system packages:
   `sudo apt install python3 python3-venv nginx curl`.
2. Create the service account:
   `sudo useradd --system --home /var/lib/webstats --shell /usr/sbin/nologin webstats`
   and `sudo usermod -a -G adm webstats`.
3. Copy this repository to `/opt/webstats` and set ownership:
   `sudo chown -R root:root /opt/webstats`.
4. Create the virtual environment and install the app:
   `sudo python3 -m venv /opt/webstats/.venv` then
   `sudo /opt/webstats/.venv/bin/pip install /opt/webstats`.
5. Create application directories:
   `sudo install -d -o root -g webstats -m 0750 /etc/webstats` and
   `sudo install -d -o webstats -g webstats -m 0750 /var/lib/webstats`.
6. Install the configuration:
   `sudo install -o root -g webstats -m 0640 /opt/webstats/config.example.toml /etc/webstats/config.toml`.
7. Edit `/etc/webstats/config.toml`, replace `secret_key` with
   `openssl rand -hex 32`, and confirm the shared host-prefixed log settings.
8. Set the admin password:
   `sudo /opt/webstats/.venv/bin/python /opt/webstats/scripts/set_password.py --config /etc/webstats/config.toml`.
9. Install `deploy/nginx-webstats-log.conf` in `/etc/nginx/conf.d`, create the
   log with `www-data:adm` ownership, enable the privacy-reduced Ad Fontes log as
   described below, and reload nginx.
10. Test parsing against the new log:
   `sudo -u webstats /opt/webstats/.venv/bin/python /opt/webstats/scripts/backfill.py --config /etc/webstats/config.toml`.
11. Install the units:
    `sudo cp /opt/webstats/deploy/webstats*.service /opt/webstats/deploy/webstats*.timer /etc/systemd/system/`
    then `sudo systemctl daemon-reload`.
12. Start ingestion and the dashboard:
    `sudo systemctl enable --now webstats-ingest.timer webstats-chronicle.timer webstats.service`.
13. Install `deploy/nginx-stats-http.conf` as the temporary vhost, reload nginx,
    obtain the certificate with the server's existing Certbot nginx convention,
    then replace it with `deploy/nginx-stats.conf`. Adjust certificate paths if
    Certbot chose a different certificate name.
14. Validate with `sudo nginx -t`, reload nginx, verify
    `https://stats.herzogenclave.com/api/health`, sign in, and add that URL to
    the existing uptime monitor.

## Configuration

Copy [`config.example.toml`](config.example.toml) and edit it outside the source
tree. Adding a site requires only another `[[sites]]` block and its log path.

`logs.format = "combined"` uses the standard format:

```text
$remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent "$http_referer" "$http_user_agent"
```

Production uses `combined_host` and assigns the shared
`/var/log/nginx/webstats.access.log` path to every site. An explicit format
string is also accepted. Required fields
are `$remote_addr`, `$time_local`, `$status`, `$http_user_agent`, and either
`$request` or both `$request_method` and `$request_uri`. Unknown variables are
matched and ignored.
The supplied production nginx formats append `$request_time`; legacy lines
without that suffix remain parseable by `combined_host` during rotation.

GeoIP is off by default. To enable it, install the optional dependency with
`pip install '/opt/webstats[geoip]'`, place a MaxMind-compatible country database
at the configured path, and set `geoip.enabled = true`. The recommended free
source is the monthly DB-IP Country Lite MMDB database. When GeoIP is enabled,
the site page displays the attribution required by DB-IP's CC BY 4.0 license.
Country lookup happens in memory during ingestion; Webstats still stores no raw
IP addresses. Existing database rows cannot be enriched after the fact, but
retained nginx logs can be reimported into a fresh database.

After installing the `geoip` dependency and enabling GeoIP, install and enable
`deploy/webstats-geoip-update.service` and
`deploy/webstats-geoip-update.timer`. The daily timer downloads at most one
database per monthly release, validates a known country lookup, and atomically
replaces the active file. Daily checks allow an automatic retry if a new release
is not yet available. No Webstats restart is needed after a database update.

## Development

Use Python 3.11 or newer:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
cp config.example.toml config.toml
WEBSTATS_CONFIG=$PWD/config.toml WEBSTATS_INSECURE_COOKIE=1 .venv/bin/python -m webstats.app --debug
```

`WEBSTATS_INSECURE_COOKIE=1` is only for local HTTP development. Production
defaults to secure cookies and must run behind HTTPS.

## Production nginx traffic log

Install the selected-host logging configuration and prepare its file before
reloading nginx:

```sh
sudo install -o root -g root -m 0644 \
  /opt/webstats/deploy/nginx-webstats-log.conf \
  /etc/nginx/conf.d/webstats-log.conf
sudo touch /var/log/nginx/webstats.access.log
sudo chown www-data:adm /var/log/nginx/webstats.access.log
sudo chmod 0640 /var/log/nginx/webstats.access.log
```

The map includes only the six ordinary sites. It deliberately excludes Ad
Fontes from the inherited log, as well as Fuse, Wordfall, unknown hosts, and
IP-address scans.

In the HTTPS `server` block for `ad-fontes.app`, replace:

```nginx
access_log off;
```

with:

```nginx
access_log /var/log/nginx/webstats.access.log webstats_private_host;
```

The private format uses `$uri`, not `$request_uri`, so query strings never reach
the log. It also writes `-` instead of the referrer. Both formats append only
nginx's aggregate request duration. Test before reloading:

```sh
sudo nginx -t
sudo systemctl reload nginx
```

Existing `/var/log/nginx/access.log` history cannot be separated by host and is
not imported. Webstats history begins when the dedicated log is enabled.

## Operations

- Run one ingest pass:
  `/opt/webstats/.venv/bin/python -m webstats.ingest --config /etc/webstats/config.toml`.
- Import current and rotated logs:
  `/opt/webstats/.venv/bin/python scripts/backfill.py --config /etc/webstats/config.toml`.
- Check local health: `scripts/healthcheck.sh`.
- Follow app logs: `journalctl -u webstats.service -f`.
- Follow ingest logs: `journalctl -u webstats-ingest.service -f`.
- Inspect timer state: `systemctl list-timers webstats-ingest.timer`.
- Run a Chronicle observation now:
  `/opt/webstats/.venv/bin/python -m webstats.chronicle --config /etc/webstats/config.toml`.
- Inspect Chronicle observations: `systemctl list-timers webstats-chronicle.timer`
  and `journalctl -u webstats-chronicle.service`.
- Inspect GeoIP updates: `systemctl list-timers webstats-geoip-update.timer` and
  `journalctl -u webstats-geoip-update.service`.
- Change the password by rerunning `scripts/set_password.py`.

The ingest state file is updated atomically after each configured log. Missing
logs produce warnings and do not stop other sites. Unparseable lines are counted,
sampled in the journal, and skipped. The health API reveals log identifiers and
offsets but not filesystem paths.

Backfill reconciles live, renamed, and gzip-compressed copies of the same log
records. It will not replace a retained historical rollup with partial archive
data after the corresponding raw rows have expired.

Database schema upgrades run automatically inside a serialized SQLite
transaction during application or ingestion startup. Back up the database before
deploying a release that changes the schema.

## API

All routes require the admin session except `/api/health`:

- `GET /api/sites`
- `GET /api/overview?from=&to=&bots=0&assets=0`
- `GET /api/site/<name>/timeseries?from=&to=&interval=day|hour`
- `GET /api/site/<name>/investigation?interval=day|hour&bucket=` opens a
  Traffic Detective case file for a chart point. Detail requires retained raw
  requests; expired periods return HTTP 410.
- `GET /api/site/<name>/pages`, `/referrers`, `/status`, `/agents`, `/countries`
- `GET /api/site/<name>/page?path=/requested/path`
- `GET /api/events?from=&to=&site=&limit=`, `/ai-crawlers`
- `GET /api/inbox?from=&to=&site=&category=&limit=`
- `GET /api/feed-readers`
- `GET /api/almanac?year=&site=&bots=0&assets=0`
- `GET /api/briefing?week=YYYY-MM-DD` (the week must begin on Monday)
- `GET /api/pulse?site=&limit=`
- `GET /api/episodes?from=&to=&site=&limit=`
- `GET /api/reliability?from=&to=&site=&limit=`
- `GET /api/chronicle?from=&to=&site=&kind=&q=&limit=`
- `GET /api/errors?site=&days=7|30|90&limit=`
- `GET /api/journeys?from=&to=&site=`
- `GET /api/link-atlas?from=&to=&site=&source=&limit=`
- `GET /api/galaxy?from=&to=&site=&limit=`
- `GET /api/live?minutes=60&limit=40`
- `GET /api/health`

Dates use `YYYY-MM-DD`. Visitor-day totals are sums of daily unique hashes. They
deliberately do not identify the same visitor across days. Site detail views use
hourly buckets automatically when a single day is selected.
Cross-property endpoints accept an optional `site=` query parameter and reject
unknown sites.

## Troubleshooting

**No data appears:** confirm the timer ran, the service account can read every
configured log, and the requested dashboard dates overlap imported data.

**Parse failures are high:** compare `logs.format` with `nginx -T`. Test an
explicit format locally before restarting the timer. A healthy deployment should
keep failures below 0.1 percent.

**Rotation misses lines:** confirm logrotate leaves the previous file as an
uncompressed `.1` for one cycle. Gzip files are supported by backfill but are not
tailed incrementally.

**Health reports degraded:** the database may be new or the latest ingest may
have failed. Inspect `journalctl -u webstats-ingest.service` and verify free disk
space and SQLite directory ownership.

**Login loops on local HTTP:** use the development-only
`WEBSTATS_INSECURE_COOKIE=1` environment variable. Never set it in production.

## Security and privacy

The web process binds to localhost. nginx handles TLS and security headers. The
service account is not root, the units restrict filesystem writes, and the config
is readable only by root and the service group. Raw IPs are transformed in
memory using a secret-derived daily salt. Raw request rows expire after the
configured retention period, while aggregates remain. Referrer and user-agent
strings are retained because they already exist in access logs.

Review [`docs/DISCOVERY.md`](docs/DISCOVERY.md) before deployment. The initial
discovery ran on the local development Mac, not the Hetzner server, so production
log paths and conventions still require read-only verification.
