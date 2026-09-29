#!/usr/bin/env python3
"""Import current and rotated logs, including gzip archives."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
import gzip
import logging
import os
from pathlib import Path
from typing import Iterator, Optional
from zoneinfo import ZoneInfo

from webstats.config import load_config
from webstats.db import connect, initialize, insert_requests, site_id_map
from webstats.geo import GeoLookup
from webstats.ingest import (
    IngestStats,
    _record_tuple,
    _select_site,
    _source_fingerprint,
    _source_key,
)
from webstats.logformat import compile_log_format
from webstats.parser import ParseError, ParsedRequest, parse_line
from webstats.rollup import maintain_rollups


LOG = logging.getLogger("webstats.backfill")


def lines_from(path: Path) -> Iterator[tuple[int, Optional[int], Optional[int], bytes]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as handle:
        inode = None if path.suffix == ".gz" else os.fstat(handle.fileno()).st_ino
        number = 0
        while True:
            offset = handle.tell()
            line = handle.readline()
            if not line:
                break
            number += 1
            yield number, None if inode is None else offset, inode, line


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
    archive_occurrences: dict[tuple[int, str], int] = defaultdict(int)
    existing_fingerprints = {
        (row["site_id"], row["fingerprint"]): row["requests"]
        for row in conn.execute(
            """
            SELECT site_id, source_fingerprint fingerprint, COUNT(*) requests
            FROM requests GROUP BY site_id, source_fingerprint
            """
        )
    }
    cutoff = (
        datetime.now(ZoneInfo(config.server.timezone)).date()
        - timedelta(days=config.storage.raw_retention_days)
    ).isoformat()
    protected_days = {
        row["day"]
        for row in conn.execute(
            "SELECT day FROM rollup_days WHERE state='sealed' AND day < ?",
            (cutoff,),
        )
    }
    protected_lines = 0
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
                    for line_number, line_offset, inode, raw in lines_from(path):
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
                            if row.day in protected_days:
                                protected_lines += 1
                                continue
                            if config.geoip.enabled:
                                match = compiled.pattern.match(text)
                                country = geo.country(match.group("remote_addr") if match else "")
                                if country:
                                    row = ParsedRequest(**{**row.__dict__, "country": country})
                            site_id = ids[selected]
                            fingerprint = _source_fingerprint(raw)
                            if inode is not None and line_offset is not None:
                                # An uncompressed rotated file is the same logical
                                # source as the configured live log. Using the base
                                # path preserves keys written before rotation.
                                source = _source_key(base, inode, line_offset, raw)
                                legacy_source = _source_key(
                                    path, inode, line_offset, raw
                                )
                                # Older ingest versions used the physical `.1`
                                # path while finishing a rotated file. Recognize
                                # that exact key so the first post-upgrade backfill
                                # cannot duplicate an already stored tail line.
                                if legacy_source != source and conn.execute(
                                    "SELECT 1 FROM requests WHERE source_key=?",
                                    (legacy_source,),
                                ).fetchone():
                                    stats.parsed += 1
                                    continue
                            else:
                                occurrence_key = (site_id, fingerprint)
                                archive_occurrences[occurrence_key] += 1
                                occurrence = archive_occurrences[occurrence_key]
                                if occurrence <= existing_fingerprints.get(
                                    occurrence_key, 0
                                ):
                                    stats.parsed += 1
                                    continue
                                source = (
                                    f"content:{site_id}:{occurrence}:{fingerprint}"
                                )
                            rows.append(_record_tuple(source, site_id, row))
                            stats.parsed += 1
                            stats.affected_days.add(row.day)
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
            conn, config.storage.raw_retention_days, stats.affected_days,
            timezone_name=config.server.timezone,
        )
        if protected_lines:
            LOG.warning(
                "Skipped %d archived lines on historical days whose complete "
                "rollups are already retained",
                protected_lines,
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
