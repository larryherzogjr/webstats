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
- A ten-second Live Radar with a rolling human activity stream, Automatic
  Moment badges, relative timestamps, and a self-contained world pulse map.
  Privacy-sensitive sites remain visible only as anonymous aggregate totals.
- An RSS readership view that recognizes hosted and self-hosted feed clients,
  charts explicitly reported subscription totals, and keeps unreported readers
  visible without inventing subscriber counts from fetch frequency.
- A Traffic Almanac with site and server-wide year heatmaps, all-time records,
  active-day streaks, record-breaking days, and visitor-day milestones.
- Automatic Weekly Briefings that compare like-for-like periods, explain the
  largest changes in plain language, collect discoveries and unusual moments,
  and provide a migration-free archive generated from permanent rollups.
- Clickable per-page stories with permanent daily history, lifetime first and
  last sightings, referrers, AI readers, countries, and response codes.
- Idempotent daily rollups retained after raw request pruning.
- Authenticated JSON APIs and responsive server-rendered pages.
- Bookmarkable all-site or single-site scopes across Live Radar, Weekly
  Briefings, AI Crawlers, Feeds, and the Traffic Almanac.
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

RSS subscriber totals are deliberately conservative. Inoreader and some other
services include an explicit subscriber count in their fetcher user agent;
Webstats records that reported number. Readers such as current Feedly identify
their fetcher without promising a count, so Webstats shows the reader and its
feed requests but labels the subscriber total as unreported. A shared fetch is
not treated as one person, and repeated fetches are never used as a proxy for
subscriber growth.

The Live Radar always excludes `ad-fontes.app` from individual activity and
country results at the API layer. Its aggregate request, visitor, and bandwidth
totals remain available without exposing paths, timestamps, or geography.

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
    `sudo systemctl enable --now webstats-ingest.timer webstats.service`.
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
the log. It also writes `-` instead of the referrer. Test before reloading:

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
- `GET /api/site/<name>/pages`, `/referrers`, `/status`, `/agents`, `/countries`
- `GET /api/site/<name>/page?path=/requested/path`
- `GET /api/events?from=&to=&site=&limit=`, `/ai-crawlers`
- `GET /api/feed-readers`
- `GET /api/almanac?year=&site=&bots=0&assets=0`
- `GET /api/briefing?week=YYYY-MM-DD` (the week must begin on Monday)
- `GET /api/live?minutes=60&limit=40`
- `GET /api/health`

Dates use `YYYY-MM-DD`. Visitor-day totals are sums of daily unique hashes. They
deliberately do not identify the same visitor across days. Site detail views use
hourly buckets automatically when a single day is selected.
Cross-property endpoints accept an optional `site=` query parameter and reject
unknown sites. Live Radar never returns individual activity or geography for a
privacy-protected site, even when that site is selected explicitly.

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
