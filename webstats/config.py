"""TOML configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 development fallback
    import tomli as tomllib  # type: ignore


class ConfigError(ValueError):
    """Raised when configuration is incomplete or unsafe."""


@dataclass(frozen=True)
class ServerConfig:
    bind: str
    secret_key: str
    admin_user: str
    admin_password_hash: str
    timezone: str


@dataclass(frozen=True)
class StorageConfig:
    db_path: Path
    state_path: Path
    raw_retention_days: int


@dataclass(frozen=True)
class GeoIPConfig:
    enabled: bool
    db_path: Path


@dataclass(frozen=True)
class SiteConfig:
    name: str
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    storage: StorageConfig
    geoip: GeoIPConfig
    ip_hash_salt_rotation: str
    log_format: str
    sites: tuple[SiteConfig, ...]
    source_path: Path


def _table(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"Missing [{name}] configuration table")
    return value


def _text(table: Dict[str, Any], key: str, section: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{section}.{key} must be a non-empty string")
    return value.strip()


def load_config(path: str | Path) -> Config:
    source = Path(path).expanduser().resolve()
    try:
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {source}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {source}: {exc}") from exc

    server = _table(raw, "server")
    storage = _table(raw, "storage")
    geoip = _table(raw, "geoip")
    privacy = _table(raw, "privacy")
    logs = _table(raw, "logs")

    rotation = _text(privacy, "ip_hash_salt_rotation", "privacy")
    if rotation != "daily":
        raise ConfigError("privacy.ip_hash_salt_rotation must be 'daily'")

    retention = storage.get("raw_retention_days")
    if not isinstance(retention, int) or retention < 1:
        raise ConfigError("storage.raw_retention_days must be a positive integer")

    log_format = _text(logs, "format", "logs")
    host_routed = "$host" in log_format or log_format in {
        "combined_host", "host_combined", "combined with $host prefix"
    }
    site_rows = raw.get("sites")
    if not isinstance(site_rows, list) or not site_rows:
        raise ConfigError("At least one [[sites]] entry is required")
    sites: List[SiteConfig] = []
    names = set()
    log_paths = set()
    for index, row in enumerate(site_rows, 1):
        if not isinstance(row, dict):
            raise ConfigError(f"sites entry {index} must be a table")
        name = _text(row, "name", f"sites[{index}]").lower().rstrip(".")
        paths = row.get("paths")
        if name in names:
            raise ConfigError(f"Duplicate site name: {name}")
        if not isinstance(paths, list) or not paths or not all(
            isinstance(item, str) and item.strip() for item in paths
        ):
            raise ConfigError(f"sites[{index}].paths must be a non-empty string list")
        resolved = tuple(Path(item).expanduser() for item in paths)
        for log_path in resolved:
            key = str(log_path)
            if key in log_paths and not host_routed:
                raise ConfigError(
                    f"Log path {log_path} is assigned more than once without a host field"
                )
            log_paths.add(key)
        names.add(name)
        sites.append(SiteConfig(name=name, paths=resolved))

    return Config(
        server=ServerConfig(
            bind=_text(server, "bind", "server"),
            secret_key=_text(server, "secret_key", "server"),
            admin_user=_text(server, "admin_user", "server"),
            admin_password_hash=_text(
                server, "admin_password_hash", "server"
            ),
            timezone=_text(server, "timezone", "server"),
        ),
        storage=StorageConfig(
            db_path=Path(_text(storage, "db_path", "storage")).expanduser(),
            state_path=Path(_text(storage, "state_path", "storage")).expanduser(),
            raw_retention_days=retention,
        ),
        geoip=GeoIPConfig(
            enabled=bool(geoip.get("enabled", False)),
            db_path=Path(_text(geoip, "db_path", "geoip")).expanduser(),
        ),
        ip_hash_salt_rotation=rotation,
        log_format=log_format,
        sites=tuple(sites),
        source_path=source,
    )
