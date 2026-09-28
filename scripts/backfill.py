#!/usr/bin/env python3
"""Import current and rotated logs, including gzip archives."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import logging
from pathlib import Path
from typing import Iterator, Optional

from webstats.config import load_config
from webstats.db import connect, initialize, insert_requests, site_id_map
from webstats.geo import GeoLookup
from webstats.ingest import IngestStats, _record_tuple, _select_site
from webstats.logformat import compile_log_format
from webstats.parser import ParseError, ParsedRequest, parse_line
from webstats.rollup import maintain_rollups


LOG = logging.getLogger("webstats.backfill")


def lines_from(path: Path) -> Iterator[tuple[int, bytes]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as handle:
        for number, line in enumerate(handle, 1):
            yield number, line


def discover_archives(base: Path) -> list[Path]:
    found = [path for path in base.parent.glob(base.name + "*") if path.is_file()]
    return sorted(found, key=lambda item: (item == base, item.name))


def run(config_path: str) -> IngestStats:
    config = load_config(config_path)
    compiled = compile_log_format(config.log_format)
    conn = connect(config.storage.db_path)
    initialize(conn, config)
    ids = site_id_map(conn)
    geo = GeoLookup(config.geoip.enabled, config.geoip.db_path)
    stats = IngestStats()
    seen_paths = set()
    try:
        for site in config.sites:
            for base in site.paths:
                for path in discover_archives(base):
                    key = str(path.resolve())
                    if key in seen_paths:
                        continue
                    seen_paths.add(key)
                    LOG.info("Importing %s", path)
                    rows = []
                    for line_number, raw in lines_from(path):
                        stats.lines_seen += 1
                        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        try:
                            row = parse_line(
                                text, compiled, site.name, config.server.secret_key,
                                timezone_name=config.server.timezone,
                            )
                            selected = _select_site(row.host, site.name, ids, compiled)
                            if selected is None:
                                raise ParseError(f"Unknown host {row.host!r}")
                            if config.geoip.enabled:
                                match = compiled.pattern.match(text)
                                country = geo.country(match.group("remote_addr") if match else "")
                                if country:
                                    row = ParsedRequest(**{**row.__dict__, "country": country})
                            digest = hashlib.sha256(raw).hexdigest()[:16]
                            source = f"backfill:{path.resolve()}:{line_number}:{digest}"
                            rows.append(_record_tuple(source, ids[selected], row))
                            stats.parsed += 1
                        except ParseError as exc:
                            stats.parse_failures += 1
                            if stats.parse_failures <= 10:
                                LOG.warning("%s:%d: %s", path, line_number, exc)
                        if len(rows) >= 1000:
                            stats.inserted += insert_requests(conn, rows)
                            conn.commit()
                            rows.clear()
                    stats.inserted += insert_requests(conn, rows)
                    conn.commit()
        maintain_rollups(
            conn, config.storage.raw_retention_days,
            timezone_name=config.server.timezone,
        )
    finally:
        geo.close()
        conn.close()
    return stats


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/webstats/config.toml")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    stats = run(args.config)
    LOG.info(
        "Seen %d lines, parsed %d, failed %d, inserted %d",
        stats.lines_seen, stats.parsed, stats.parse_failures, stats.inserted,
    )
    return 0 if stats.parse_failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
