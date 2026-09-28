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
- Idempotent daily rollups retained after raw request pruning.
- Authenticated JSON APIs and responsive server-rendered pages.
- Local Chart.js 4.4.7 bundle with no CDN or front-end build step.
- Bcrypt login, secure session cookies, login throttling, and public safe health
  status.
- systemd, nginx, health-check, password, and backfill assets.

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
    `sudo cp /opt/webstats/deploy/webstats*.service /opt/webstats/deploy/webstats-ingest.timer /etc/systemd/system/`
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
at the configured path, and set `geoip.enabled = true`.

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
- Change the password by rerunning `scripts/set_password.py`.

The ingest state file is updated atomically after each configured log. Missing
logs produce warnings and do not stop other sites. Unparseable lines are counted,
sampled in the journal, and skipped. The health API reveals log identifiers and
offsets but not filesystem paths.

## API

All routes require the admin session except `/api/health`:

- `GET /api/sites`
- `GET /api/overview?from=&to=&bots=0&assets=0`
- `GET /api/site/<name>/timeseries?from=&to=&interval=day|hour`
- `GET /api/site/<name>/pages`, `/referrers`, `/status`, `/agents`, `/countries`
- `GET /api/live?minutes=60`
- `GET /api/health`

Dates use `YYYY-MM-DD`. Daily visitor totals are sums of daily unique hashes.
They deliberately do not identify the same visitor across days.

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
