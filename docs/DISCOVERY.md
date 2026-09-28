# Deployment discovery

Date: 2026-09-28

## Scope and limitation

Discovery ran in the Codex development workspace on `iMav-MBA.local`, not on the
Hetzner deployment server. The deployment server is not connected to this
workspace, so its nginx vhosts and access logs could not be inspected. The
application remains fully configuration-driven, and the commands below make the
remaining server-side verification explicit.

## Findings on the available host

- Operating system: macOS 26.6.2, arm64.
- Python: 3.9.6. Production requires Python 3.11 or newer. The source remains
  compatible with Python 3.9 so parser and database tests can run locally.
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

## Configuration decision

`config.example.toml` uses the per-vhost nginx paths from the approved plan and
the standard nginx `combined` format. These are deployment defaults, not claimed
discoveries. Every path and the format must be checked on the Hetzner server
before enabling the ingest timer.

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

## Items requiring production confirmation

- Exact access-log path for each configured site.
- Whether the active format is standard `combined`, host-prefixed `combined`, or
  a custom `log_format` string.
- Whether each site has its own log or all sites share one host-prefixed log.
- Rotation naming and frequency, including whether `.1` is left uncompressed for
  one cycle.
- The service account and group conventions already used on the server.
- Availability of Python 3.11 or newer and gunicorn deployment conventions.
- Certbot certificate name and TLS include paths for `stats.herzogenclave.com`.

