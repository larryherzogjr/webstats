# Web Statistics Dashboard: Project Plan for Codex

**Owner:** Larry Herzog Jr.
**Host:** Hetzner bare-metal server (single box, all sites and all logs local)
**Date:** 2026-09-28
**Status:** Ready for implementation

---

## 1. Goal

Build a self-hosted, privacy-respecting web statistics dashboard that reads the web server access logs already on this host and presents per-site and cross-site traffic statistics in a browser. No third-party analytics, no JavaScript tracking snippet on the sites, no data leaves the server.

Sites in scope (one "site" = one vhost):

| Site | Notes |
|---|---|
| larryherzogjr.com | Author hub, essays, Greek/Hebrew explorers |
| ad-fontes.app | Ad Fontes NT application |
| pick5.ospdy.com | NFL Pick 5 app (Flask) |
| hdu.ospdy.com | Hot Death Uno game |
| euphonium.studio | Euphonium reference site |
| alexis.tips | K-12 music teacher site |
| herzogenclave.com | IT consulting practice |

The site list must live in config, not code. Adding an eighth site should be a config edit plus a log path.

---

## 2. Assumptions (proceed on these; correct them in discovery, do not stop to ask)

1. **Web server is nginx.** If discovery finds Apache or Caddy instead, adapt the log format parser and keep everything else. The log format is a config value, not a hardcoded regex.
2. **Logs are per-vhost** (one access log per site) under `/var/log/nginx/` and rotated by `logrotate` with gzip. If all sites share one combined log, use the `$host`/vhost field to split them.
3. **Python 3.11+** is available (or can be installed). Stack: Python, SQLite, Flask, Chart.js. This matches Larry's other self-hosted apps (Pick 5, Sermon Broadcaster, UniFi suite).
4. **The dashboard is for one person.** Single admin login is sufficient. No multi-tenant work.
5. **Dashboard URL** will be `stats.herzogenclave.com` behind nginx as a reverse proxy with TLS via the existing certbot setup. If Larry prefers a different hostname, only the nginx vhost changes.
6. **Retention:** raw parsed request rows for 90 days, daily aggregates forever.
7. **GeoIP** is a nice-to-have, not a requirement. Use the free DB-IP Lite or MaxMind GeoLite2 country database if a license key/download is available; otherwise ship without it and leave the hook in place.

---

## 3. Non-goals

- No client-side tracking script. Logs are the only data source.
- No real-time streaming (near-real-time via a cron ingest every 5 minutes is fine).
- No user-level tracking, sessions across days, or fingerprinting.
- No replacement of server monitoring (CPU, disk, uptime). Traffic only.
- No email reports in v1 (listed as a later enhancement).

---

## 4. Architecture

```
/var/log/nginx/*.access.log(.1|.N.gz)
        │
        ▼
  ingest.py  (cron every 5 min, or systemd timer)
   - tails each configured log incrementally (offset + inode tracking)
   - parses lines per configured log format
   - classifies bots, hashes IPs, resolves GeoIP (optional)
   - inserts into SQLite `requests`
   - rolls up into `daily_site`, `daily_path`, `daily_referrer`, `daily_agent`
        │
        ▼
  SQLite database  (/var/lib/webstats/webstats.db, WAL mode)
        │
        ▼
  Flask app  (gunicorn, systemd service, 127.0.0.1:5010)
   - JSON API under /api/
   - Jinja2 pages + Chart.js under /
   - single admin login (session cookie, bcrypt password hash in config)
        │
        ▼
  nginx reverse proxy  stats.herzogenclave.com  →  127.0.0.1:5010  (TLS, HTTP basic auth optional as a second layer)
```

Two processes, one database, no message queue, no Docker required (Docker is acceptable if it is already in use on the host; do not introduce it otherwise).

---

## 5. Repository layout

```
webstats/
├── README.md                  # install, config, run, troubleshoot
├── pyproject.toml             # or requirements.txt; pin versions
├── config.example.toml        # copy to /etc/webstats/config.toml
├── webstats/
│   ├── __init__.py
│   ├── config.py              # load + validate TOML
│   ├── logformat.py           # nginx/apache/caddy format → regex compiler
│   ├── parser.py              # line → normalized record
│   ├── bots.py                # bot / crawler classification
│   ├── geo.py                 # optional GeoIP lookup, no-op if DB absent
│   ├── ingest.py              # incremental tail, state file, rollups
│   ├── db.py                  # schema, migrations, connection helpers
│   ├── rollup.py              # daily aggregate maintenance + retention pruning
│   ├── app.py                 # Flask app factory
│   ├── api.py                 # /api/* JSON endpoints
│   ├── views.py               # HTML pages
│   ├── auth.py                # login/logout, session, password check
│   ├── templates/
│   └── static/                # chart.js (vendored), one CSS file, minimal JS
├── scripts/
│   ├── backfill.py            # one-time import of rotated .gz history
│   ├── set_password.py        # writes bcrypt hash into config
│   └── healthcheck.sh
├── deploy/
│   ├── webstats.service       # gunicorn systemd unit
│   ├── webstats-ingest.service
│   ├── webstats-ingest.timer  # every 5 minutes
│   └── nginx-stats.conf       # reverse proxy vhost
└── tests/
    ├── fixtures/              # sample log lines per format, incl. edge cases
    ├── test_parser.py
    ├── test_bots.py
    ├── test_ingest.py         # rotation, partial lines, inode change
    └── test_api.py
```

---

## 6. Configuration (`/etc/webstats/config.toml`)

```toml
[server]
bind = "127.0.0.1:5010"
secret_key = "..."              # generated at install
admin_user = "larry"
admin_password_hash = "$2b$..." # set via scripts/set_password.py
timezone = "America/Chicago"

[storage]
db_path = "/var/lib/webstats/webstats.db"
state_path = "/var/lib/webstats/ingest-state.json"
raw_retention_days = 90

[geoip]
enabled = false
db_path = "/var/lib/webstats/dbip-country-lite.mmdb"

[privacy]
ip_hash_salt_rotation = "daily"   # daily salt → unique-visitor counts per day, no cross-day tracking

[logs]
format = "combined"               # or an explicit nginx log_format string

[[sites]]
name = "larryherzogjr.com"
paths = ["/var/log/nginx/larryherzogjr.com.access.log"]

[[sites]]
name = "ad-fontes.app"
paths = ["/var/log/nginx/ad-fontes.app.access.log"]

# ... one block per site; pick5.ospdy.com, hdu.ospdy.com, euphonium.studio,
#     alexis.tips, herzogenclave.com
```

Discovery (Phase 0) fills in the real paths and format.

---

## 7. Data model

### `requests` (raw, pruned after `raw_retention_days`)
| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| site_id | INTEGER FK | |
| ts | INTEGER | unix epoch, UTC |
| ip_hash | TEXT | sha256(salt_for_day + ip), truncated to 16 hex |
| method | TEXT | |
| path | TEXT | query string stripped, stored separately if needed |
| query | TEXT | nullable |
| status | INTEGER | |
| bytes | INTEGER | |
| referrer_host | TEXT | nullable, host only |
| referrer | TEXT | full, nullable |
| user_agent | TEXT | |
| ua_family | TEXT | browser/bot family from a lightweight parser |
| is_bot | INTEGER | 0/1 |
| country | TEXT | ISO-2, nullable |

Indexes: `(site_id, ts)`, `(site_id, path)`, `(site_id, is_bot, ts)`.

### Daily rollups (kept forever)
- `daily_site(site_id, day, requests, human_requests, bot_requests, unique_visitors, bytes, status_2xx, status_3xx, status_4xx, status_5xx)`
- `daily_path(site_id, day, path, requests, unique_visitors)`
- `daily_referrer(site_id, day, referrer_host, requests)`
- `daily_agent(site_id, day, ua_family, is_bot, requests)`
- `daily_country(site_id, day, country, requests)`
- `daily_status(site_id, day, status, requests)`

Rollups are idempotent: recomputing a day from `requests` replaces that day's rows. The ingest run recomputes today and yesterday every pass.

### `ingest_state` (JSON file, not a table)
Per configured path: `inode`, `offset`, `last_run`. Handles logrotate by detecting inode change or file shrink, finishing the old file (`.1`) if it still exists, then resetting to offset 0 on the new file.

---

## 8. Ingest rules

1. Read from saved offset. If inode changed or size < offset, treat as rotated: read the rest of the previous file if `.1` (uncompressed) is present and its inode matches the saved one, then start the new file at 0.
2. Skip the trailing partial line; do not advance offset past it.
3. Parse with the compiled log-format regex. Unparseable lines go to a counter and a sampled log entry, never a crash.
4. Normalize:
   - `path`: strip query string, collapse duplicate slashes, cap at 512 chars.
   - `referrer_host`: parse host, drop self-referrals (referrer host equals the site name or `www.` variant).
   - Static-asset requests (`.css .js .png .jpg .svg .woff2 .ico .map`) are stored but flagged via `ua_family`/path filter so the dashboard can exclude them by default.
5. Bot classification (`bots.py`):
   - Known crawler substrings (Googlebot, bingbot, DuckDuckBot, Applebot, AhrefsBot, SemrushBot, PetalBot, Bytespider, GPTBot, ClaudeBot, CCBot, facebookexternalhit, etc.), maintained as a plain list in the module.
   - Generic signals: empty UA, `python-requests`, `curl`, `Go-http-client`, `Scrapy`, `HeadlessChrome`, no `Mozilla/` prefix.
   - Behavioral (optional, Phase 3): more than N requests per minute from one ip_hash, or repeated 404 probing of `/wp-login.php`, `/.env`, `/xmlrpc.php`.
6. IP hashing: salt = sha256(secret_key + YYYY-MM-DD). The raw IP is never written to disk.
7. After inserts, run rollups for affected days and prune `requests` older than retention.

---

## 9. Dashboard pages

All pages default to **humans only, static assets excluded**, with toggles to include bots and assets. Date range picker: today, 7d, 30d, 90d, custom. Timezone display: America/Chicago.

1. **Overview** (`/`)
   - One card per site: requests, unique visitors, change vs. prior period.
   - Combined line chart: daily human requests, all sites stacked or overlaid.
   - Server-wide: total bandwidth, error rate, bot share.
2. **Site detail** (`/site/<name>`)
   - Traffic over time (requests + unique visitors).
   - Top pages (path, requests, uniques), paginated.
   - Top referrers (host), with search-engine grouping.
   - Status code breakdown; list of top 404 paths (useful for catching broken links after essay moves).
   - Browsers / OS families.
   - Countries (if GeoIP enabled).
   - Bot breakdown: which crawlers, how much.
3. **Realtime-ish** (`/live`)
   - Last 60 minutes, refreshed every 60 seconds via `/api/live`, per site.
4. **Health** (`/health`)
   - Last ingest run, lines parsed, parse failures, DB size, per-log offsets. Also exposed as plain JSON at `/api/health` for uptime checks (no auth required, no sensitive data).

Keep the UI plain: one stylesheet, Chart.js vendored locally (no CDN), no build step, no front-end framework.

---

## 10. API (all JSON, all behind auth except `/api/health`)

- `GET /api/sites`
- `GET /api/overview?from=&to=&bots=0&assets=0`
- `GET /api/site/<name>/timeseries?from=&to=&interval=day|hour`
- `GET /api/site/<name>/pages?from=&to=&limit=&offset=`
- `GET /api/site/<name>/referrers?...`
- `GET /api/site/<name>/status?...`
- `GET /api/site/<name>/agents?...`
- `GET /api/site/<name>/countries?...`
- `GET /api/live?minutes=60`
- `GET /api/health`

Hourly interval queries hit `requests` (limited to retention window); daily queries hit rollups.

---

## 11. Security

- App binds to localhost only; nginx terminates TLS.
- Single admin account, bcrypt hash, session cookie `Secure; HttpOnly; SameSite=Lax`.
- Rate-limit `/login` (simple in-memory counter is fine for one user).
- Optional second layer: nginx `auth_basic` on the vhost.
- Ingest runs as a dedicated `webstats` system user with read access to the log directory (add to the `adm` group on Debian/Ubuntu, or grant an ACL). It must not run as root.
- Config file mode `0640`, owned `root:webstats`.
- No raw IPs stored. Referrer and UA strings are stored as-is; they are already in the logs.

---

## 12. Deployment

1. `useradd -r -s /usr/sbin/nologin webstats`; add to `adm`.
2. Install to `/opt/webstats` in a venv; config in `/etc/webstats/`; data in `/var/lib/webstats/`.
3. `systemd`: `webstats.service` (gunicorn, 2 workers) and `webstats-ingest.timer` (every 5 min, `Persistent=true`).
4. nginx vhost for `stats.herzogenclave.com` proxying to `127.0.0.1:5010`; certbot for TLS.
5. Run `scripts/backfill.py` once to import rotated history (`.gz` files) so the dashboard is not empty on day one.
6. Add `/api/health` to whatever uptime monitor is already in use.

---

## 13. Phases and acceptance criteria

### Phase 0: Discovery (do this first, report findings, then continue)
- Identify web server, version, log directory, log format directive, rotation policy, which vhost logs to which file.
- Confirm Python version, whether Docker is present, existing systemd/nginx conventions on the host (match them).
- Output: fill in `config.example.toml` with real paths; write findings to `docs/DISCOVERY.md`.

### Phase 1: Parser and ingest
- Parser handles the discovered format plus `combined` and `combined` with `$host` prefix.
- Ingest survives: rotation mid-run, gzip backfill, partial trailing line, duplicate run (no double counting).
- Tests pass on fixtures. `backfill.py` imports all history without error.
- Acceptance: `SELECT count(*) FROM requests` matches `wc -l` of the ingested logs minus parse failures, and the failure rate is under 0.1%.

### Phase 2: Rollups and API
- All daily tables populated; recompute is idempotent.
- All `/api/*` endpoints return correct numbers for a hand-checked day (compare against `grep | wc -l` on the raw log for one site, one day).
- Retention pruning works and does not touch rollups.

### Phase 3: Dashboard UI
- Overview, site detail, live, health pages functional.
- Human/bot and asset toggles work everywhere.
- Renders acceptably on a phone.

### Phase 4: Deployment and hardening
- systemd units, nginx vhost, dedicated user, TLS, login, health check.
- README covers fresh install in under 15 steps.

### Later (not v1)
- Weekly email summary.
- Behavioral bot detection.
- Per-essay "reads" view for larryherzogjr.com (path grouping by section).
- Export to CSV.

---

## 14. Working conventions for Codex

- Commit after each phase with a message naming the phase.
- Write tests before marking a phase complete; do not skip the rotation tests.
- Do not add dependencies beyond: Flask, gunicorn, bcrypt, (optional) `maxminddb`, and a small UA parser such as `ua-parser`. No ORM. Plain `sqlite3` with a thin helper.
- Proceed on the assumptions in section 2. When a discovery finding contradicts one, adapt, note it in `docs/DISCOVERY.md`, and keep going. Only stop for something destructive or for a missing credential (for example a GeoIP license key).
- Never modify the existing site vhosts or their log configuration. Read-only against the logs.
- No em dashes in any documentation, comments, or UI copy.
