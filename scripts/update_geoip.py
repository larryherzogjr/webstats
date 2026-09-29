#!/usr/bin/env python3
"""Download and atomically install the current DB-IP Country Lite database."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import os
from pathlib import Path
import re
import tempfile
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen


MAX_COMPRESSED_BYTES = 32 * 1024 * 1024
MAX_DATABASE_BYTES = 64 * 1024 * 1024
RELEASE_PATTERN = re.compile(r"^\d{4}-\d{2}$")
USER_AGENT = "webstats-geoip/1.0 (+https://github.com/larryherzogjr/webstats)"


def current_release(now: Optional[datetime] = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m")


def database_url(release: str) -> str:
    if not RELEASE_PATTERN.fullmatch(release):
        raise ValueError("Release must use YYYY-MM")
    return f"https://download.db-ip.com/free/dbip-country-lite-{release}.mmdb.gz"


def validate_database(path: Path) -> None:
    try:
        import maxminddb
    except ImportError as exc:
        raise RuntimeError("The maxminddb package is not installed") from exc
    reader = maxminddb.open_database(str(path))
    try:
        result = reader.get("8.8.8.8") or {}
        code = result.get("country", {}).get("iso_code")
        if not isinstance(code, str) or len(code) != 2:
            raise RuntimeError("Downloaded database failed its country lookup check")
    finally:
        reader.close()


def _copy_bounded(source, destination, limit: int) -> None:
    total = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise RuntimeError("Downloaded GeoIP data exceeds the safety limit")
        destination.write(chunk)


def _download(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response, destination.open("wb") as output:
        _copy_bounded(response, output, MAX_COMPRESSED_BYTES)
        output.flush()
        os.fsync(output.fileno())


def _decompress(source: Path, destination: Path) -> None:
    with gzip.open(source, "rb") as compressed, destination.open("wb") as output:
        _copy_bounded(compressed, output, MAX_DATABASE_BYTES)
        output.flush()
        os.fsync(output.fileno())


def _write_stamp(path: Path, release: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(release + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def update(database: Path, stamp: Path, release: str) -> bool:
    if database.exists() and stamp.exists():
        try:
            if stamp.read_text(encoding="utf-8").strip() == release:
                validate_database(database)
                return False
        except (OSError, RuntimeError, ValueError):
            pass

    database.parent.mkdir(parents=True, exist_ok=True)
    compressed_fd, compressed_name = tempfile.mkstemp(
        dir=database.parent, prefix=database.name + ".", suffix=".gz"
    )
    database_fd, database_name = tempfile.mkstemp(
        dir=database.parent, prefix=database.name + ".", suffix=".tmp"
    )
    os.close(compressed_fd)
    os.close(database_fd)
    compressed = Path(compressed_name)
    temporary_database = Path(database_name)
    try:
        _download(database_url(release), compressed)
        _decompress(compressed, temporary_database)
        os.chmod(temporary_database, 0o640)
        validate_database(temporary_database)
        os.replace(temporary_database, database)
        _write_stamp(stamp, release)
        return True
    finally:
        compressed.unlink(missing_ok=True)
        temporary_database.unlink(missing_ok=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", default="/var/lib/webstats/dbip-country-lite.mmdb"
    )
    parser.add_argument(
        "--stamp", default="/var/lib/webstats/dbip-country-lite.release"
    )
    parser.add_argument("--release", default=current_release())
    args = parser.parse_args(argv)
    try:
        changed = update(Path(args.database), Path(args.stamp), args.release)
    except (EOFError, OSError, RuntimeError, URLError, ValueError, gzip.BadGzipFile) as exc:
        print(f"GeoIP update failed: {exc}")
        return 1
    action = "Installed" if changed else "Already current"
    print(f"{action}: DB-IP Country Lite {args.release}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
