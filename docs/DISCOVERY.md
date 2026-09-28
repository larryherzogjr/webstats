# Deployment discovery

Date: 2026-09-28

## Scope

Initial discovery ran in the Codex development workspace. Production findings
were then supplied from the Hetzner server on 2026-09-28.

## Findings on the available host

- Operating system: macOS 26.6.2, arm64.
- System Python: 3.9.6. Homebrew Python 3.12 is also available and is used for
  development. Production requires Python 3.11 or newer.
- nginx: not installed.
- Apache: the macOS system `apachectl` exists, but no evidence indicates that it
  serves the sites in scope.
- Caddy: not installed.
- Docker: 29.4.3 is installed. The project does not use it because the plan says
  not to introduce Docker solely for this service.
- systemd: unavailable on macOS. Production unit files follow standard Debian and
  Ubuntu conventions.
- `/var/log/nginx`: absent on this host.
- `/etc/nginx`: absent on this host.
- Repository: the workspace initially contained only the project plan and was not
  a Git repository.

## Production findings

- Operating system: Ubuntu 24.04.5 LTS.
- Python: 3.12.3.
- nginx: 1.24.0 from Ubuntu.
- nginx writes one global `/var/log/nginx/access.log` using the built-in combined
  format. That format does not contain `$host`, so existing history cannot be
  reliably separated by site.
- Logrotate runs daily, retains 14 rotations, compresses older files, and uses
  `delaycompress`. The immediately previous `.1` file remains uncompressed and
  is compatible with incremental rotation recovery.
- Rotated files are created with mode `0640`, owner `www-data`, and group `adm`.
- `ad-fontes.app` deliberately disables its ordinary access log to avoid storing
  OAuth codes, passage queries, or identities.
- `fuse.ospdy.com` and `wordfall.ospdy.com` are present on the server but are out
  of scope for Webstats.

## Configuration decision

`config.example.toml` now uses one dedicated host-prefixed log at
`/var/log/nginx/webstats.access.log` and the `combined_host` parser. The nginx
configuration in `deploy/nginx-webstats-log.conf` writes only the six ordinary
sites selected in its map. It excludes Ad Fontes, Fuse, Wordfall, unknown hosts,
and IP-address scans from the inherited log.

The Ad Fontes HTTPS server must replace its existing `access_log off` directive
with the privacy-reduced `webstats_private_host` log. That format records the
host, hashed-later IP source, timestamp, method, path without query string,
status, byte count, and user agent. It writes no query string or referrer.

## Production verification commands

Run these read-only commands on the Hetzner server before installation:

```sh
nginx -v
python3 --version
nginx -T 2>&1 | grep -E 'log_format|access_log|server_name'
find /var/log/nginx -maxdepth 1 -type f -print
cat /etc/logrotate.d/nginx
systemctl cat nginx
```

Record any differences here and update only `config.toml`. Do not alter existing
site vhosts or log directives.

## Remaining production confirmation

- Exact source file containing the Ad Fontes HTTPS `access_log off` directive.
- Certbot certificate name and TLS include paths for `stats.herzogenclave.com`.
