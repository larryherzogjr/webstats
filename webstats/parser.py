"""Normalize access-log lines without retaining raw IP addresses."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import SplitResult, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .bots import classify_user_agent, os_family
from .logformat import CompiledLogFormat


ASSET_EXTENSIONS = (
    ".css",
    ".js",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".avif",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".ico",
    ".map",
)


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedRequest:
    host: Optional[str]
    ts: int
    day: str
    ip_hash: str
    method: str
    path: str
    query: Optional[str]
    status: int
    bytes: int
    referrer_host: Optional[str]
    referrer: Optional[str]
    user_agent: str
    ua_family: str
    os_family: str
    is_bot: int
    is_asset: int
    country: Optional[str]


def daily_ip_hash(secret_key: str, day: str, remote_addr: str) -> str:
    salt = hashlib.sha256(f"{secret_key}:{day}".encode()).digest()
    return hashlib.sha256(salt + remote_addr.encode()).hexdigest()[:16]


def normalize_path(raw_target: str) -> tuple[str, Optional[str]]:
    try:
        parsed: SplitResult = urlsplit(raw_target)
    except ValueError as exc:
        raise ParseError(f"Invalid request target: {raw_target!r}") from exc
    raw_path = parsed.path or "/"
    while "//" in raw_path:
        raw_path = raw_path.replace("//", "/")
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path
    path = raw_path[:512]
    return path, parsed.query or None


def parse_line(
    line: str,
    log_format: CompiledLogFormat,
    site_name: str,
    secret_key: str,
    country: Optional[str] = None,
    timezone_name: str = "UTC",
) -> ParsedRequest:
    match = log_format.pattern.match(line.rstrip("\r\n"))
    if not match:
        raise ParseError("Line does not match configured log format")
    fields = match.groupdict()
    try:
        occurred = datetime.strptime(fields["time_local"], "%d/%b/%Y:%H:%M:%S %z")
    except (KeyError, ValueError) as exc:
        raise ParseError("Invalid access-log timestamp") from exc
    try:
        display_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ParseError(f"Unknown timezone: {timezone_name}") from exc
    day = occurred.astimezone(display_zone).date().isoformat()

    request = fields.get("request", "")
    if request:
        parts = request.split()
        if len(parts) < 2:
            raise ParseError("Invalid request field")
        method, target = parts[0], parts[1]
    else:
        method = fields.get("request_method") or ""
        target = fields.get("request_uri") or ""
    path, query = normalize_path(target)
    user_agent = _dash_to_empty(fields.get("http_user_agent")) or ""
    ua_family, is_bot = classify_user_agent(user_agent)
    host = _dash_to_empty(fields.get("host"))
    normalized_host = host.lower().split(":", 1)[0] if host else None
    referrer = _dash_to_empty(fields.get("http_referer"))
    referrer_host = _referrer_host(referrer, normalized_host or site_name)
    byte_value = fields.get("body_bytes_sent") or fields.get("bytes_sent") or "0"
    remote_addr = fields.get("remote_addr") or ""
    return ParsedRequest(
        host=normalized_host,
        ts=int(occurred.timestamp()),
        day=day,
        ip_hash=daily_ip_hash(secret_key, day, remote_addr),
        method=method[:16],
        path=path,
        query=query,
        status=int(fields["status"]),
        bytes=int(byte_value) if byte_value.isdigit() else 0,
        referrer_host=referrer_host,
        referrer=referrer,
        user_agent=user_agent[:1024],
        ua_family=ua_family,
        os_family=os_family(user_agent),
        is_bot=int(is_bot),
        is_asset=int(path.lower().endswith(ASSET_EXTENSIONS)),
        country=country,
    )


def _dash_to_empty(value: Optional[str]) -> Optional[str]:
    if value in {None, "", "-"}:
        return None
    return value


def _referrer_host(referrer: Optional[str], site_name: str) -> Optional[str]:
    if not referrer:
        return None
    try:
        host = (urlsplit(referrer).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    normalized_site = site_name.lower().removeprefix("www.")
    if host.removeprefix("www.") == normalized_site:
        return None
    return host or None
