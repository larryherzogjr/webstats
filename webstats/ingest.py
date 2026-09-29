"""Incrementally ingest configured access logs."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, Optional

from .config import Config, SiteConfig, load_config
from .db import connect, initialize, insert_requests, site_id_map
from .geo import GeoLookup
from .logformat import CompiledLogFormat, compile_log_format
from .parser import ParseError, ParsedRequest, parse_line


LOG = logging.getLogger("webstats.ingest")


@dataclass
class IngestStats:
    lines_seen: int = 0
    parsed: int = 0
    parse_failures: int = 0
    inserted: int = 0
    affected_days: set[str] = field(default_factory=set)


def load_state(path: Path) -> Dict[str, dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Cannot read ingest state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Ingest state {path} must contain an object")
    return value


def save_state(path: Path, state: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, path)


def _complete_lines(path: Path, offset: int) -> tuple[list[tuple[int, bytes]], int]:
    lines: list[tuple[int, bytes]] = []
    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            start = handle.tell()
            line = handle.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                handle.seek(start)
                break
            lines.append((start, line))
        return lines, handle.tell()


def _rotated_candidate(path: Path, inode: int) -> Optional[Path]:
    candidates = [Path(str(path) + ".1"), path.with_name(path.stem + ".1" + path.suffix)]
    for candidate in candidates:
        try:
            if candidate.stat().st_ino == inode:
                return candidate
        except FileNotFoundError:
            continue
    return None


def _source_key(path: Path, inode: int, offset: int, raw: bytes) -> str:
    fingerprint = _source_fingerprint(raw)
    return f"{path}:{inode}:{offset}:{fingerprint}"


def _source_fingerprint(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def _record_tuple(source_key: str, site_id: int, row: ParsedRequest) -> tuple:
    return (
        source_key,
        source_key.rsplit(":", 1)[-1],
        site_id,
        row.ts,
        row.day,
        row.ip_hash,
        row.method,
        row.path,
        row.query,
        row.status,
        row.bytes,
        row.referrer_host,
        row.referrer,
        row.user_agent,
        row.ua_family,
        row.os_family,
        row.is_bot,
        row.is_asset,
        row.country,
        row.request_time_ms,
    )


def ingest_once(config: Config) -> IngestStats:
    stats = IngestStats()
    log_format = compile_log_format(config.log_format)
    state = load_state(config.storage.state_path)
    conn = connect(config.storage.db_path)
    initialize(conn, config)
    sites = site_id_map(conn)
    started = int(time.time())
    run_id = conn.execute(
        "INSERT INTO ingest_runs(started_at, status) VALUES (?, 'running')", (started,)
    ).lastrowid
    conn.commit()
    geo = GeoLookup(config.geoip.enabled, config.geoip.db_path)
    try:
        for site in config.sites:
            for path in site.paths:
                _ingest_path(
                    conn, config, site, path, sites, log_format, state, geo, stats
                )
                conn.commit()
                save_state(config.storage.state_path, state)
        from .rollup import maintain_rollups

        maintain_rollups(
            conn, config.storage.raw_retention_days, stats.affected_days,
            config.server.timezone,
        )
        conn.execute(
            """
            UPDATE ingest_runs SET finished_at=?, lines_seen=?, parsed=?,
                parse_failures=?, inserted=?, status='ok' WHERE id=?
            """,
            (
                int(time.time()), stats.lines_seen, stats.parsed,
                stats.parse_failures, stats.inserted, run_id,
            ),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE ingest_runs SET finished_at=?, status='error', detail=? WHERE id=?",
            (int(time.time()), str(exc)[:1000], run_id),
        )
        conn.commit()
        raise
    finally:
        geo.close()
        conn.close()
    return stats


def _ingest_path(
    conn,
    config: Config,
    configured_site: SiteConfig,
    path: Path,
    site_ids: dict[str, int],
    log_format: CompiledLogFormat,
    state: Dict[str, dict],
    geo: GeoLookup,
    stats: IngestStats,
) -> None:
    key = str(path)
    try:
        current = path.stat()
    except FileNotFoundError:
        LOG.warning("Configured log does not exist: %s", path)
        return
    saved = state.get(key, {})
    saved_inode = int(saved.get("inode", current.st_ino))
    saved_offset = int(saved.get("offset", 0))

    if saved_inode != current.st_ino:
        rotated = _rotated_candidate(path, saved_inode)
        if rotated is not None:
            _consume_file(
                conn, config, configured_site, rotated, saved_inode, saved_offset,
                site_ids, log_format, geo, stats, identity_path=path,
            )
        saved_offset = 0
    elif current.st_size < saved_offset:
        saved_offset = 0

    new_offset = _consume_file(
        conn, config, configured_site, path, current.st_ino, saved_offset,
        site_ids, log_format, geo, stats,
    )
    state[key] = {
        "inode": current.st_ino,
        "offset": new_offset,
        "last_run": int(time.time()),
    }


def _consume_file(
    conn,
    config: Config,
    configured_site: SiteConfig,
    path: Path,
    inode: int,
    offset: int,
    site_ids: dict[str, int],
    log_format: CompiledLogFormat,
    geo: GeoLookup,
    stats: IngestStats,
    identity_path: Optional[Path] = None,
) -> int:
    lines, new_offset = _complete_lines(path, offset)
    rows = []
    for line_offset, raw in lines:
        stats.lines_seen += 1
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        try:
            row = parse_line(
                text, log_format, configured_site.name, config.server.secret_key,
                timezone_name=config.server.timezone,
            )
            selected_site = _select_site(row.host, configured_site.name, site_ids, log_format)
            if selected_site is None:
                raise ParseError(f"Unknown host {row.host!r}")
            if config.geoip.enabled:
                remote_match = log_format.pattern.match(text)
                remote_ip = remote_match.group("remote_addr") if remote_match else ""
                country = geo.country(remote_ip)
                if country:
                    row = ParsedRequest(**{**row.__dict__, "country": country})
            rows.append(
                _record_tuple(
                    _source_key(identity_path or path, inode, line_offset, raw),
                    site_ids[selected_site],
                    row,
                )
            )
            stats.parsed += 1
            stats.affected_days.add(row.day)
        except ParseError as exc:
            stats.parse_failures += 1
            if stats.parse_failures <= 10:
                LOG.warning("Unparseable line in %s at byte %d: %s", path, line_offset, exc)
    stats.inserted += insert_requests(conn, rows)
    return new_offset


def _select_site(
    host: Optional[str], configured_site: str, site_ids: dict[str, int], log_format: CompiledLogFormat
) -> Optional[str]:
    if not log_format.has_host:
        return configured_site
    if not host:
        return None
    normalized = host.removeprefix("www.")
    for name in site_ids:
        if name.removeprefix("www.") == normalized:
            return name
    return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/webstats/config.toml")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    result = ingest_once(load_config(args.config))
    LOG.info(
        "Seen %d lines, parsed %d, failed %d, inserted %d",
        result.lines_seen, result.parsed, result.parse_failures, result.inserted,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
