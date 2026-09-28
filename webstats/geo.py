"""Optional MaxMind-compatible country lookup."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


class GeoLookup:
    def __init__(self, enabled: bool, db_path: Path):
        self.reader = None
        if enabled and db_path.exists():
            try:
                import maxminddb
            except ImportError as exc:
                raise RuntimeError(
                    "GeoIP is enabled but the maxminddb extra is not installed"
                ) from exc
            self.reader = maxminddb.open_database(str(db_path))

    def country(self, ip_address: str) -> Optional[str]:
        if self.reader is None:
            return None
        try:
            result = self.reader.get(ip_address) or {}
        except (ValueError, OSError):
            return None
        code = result.get("country", {}).get("iso_code")
        return str(code).upper()[:2] if code else None

    def close(self) -> None:
        if self.reader is not None:
            self.reader.close()

