"""Compile supported nginx and Apache-style log formats into regex patterns."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Pattern


COMBINED = (
    '$remote_addr - $remote_user [$time_local] "$request" $status '
    '$body_bytes_sent "$http_referer" "$http_user_agent"'
)
COMBINED_HOST = "$host " + COMBINED

FIELD_PATTERNS: Dict[str, str] = {
    "host": r"(?P<host>[^\s]+)",
    "remote_addr": r"(?P<remote_addr>[^\s]+)",
    "remote_user": r"(?P<remote_user>[^\s]+)",
    "time_local": r"(?P<time_local>[^\]]+)",
    "request": r"(?P<request>[^\"]*)",
    "request_method": r"(?P<request_method>[^\s\"]+)",
    "request_uri": r"(?P<request_uri>[^\s\"]+)",
    "server_protocol": r"(?P<server_protocol>[^\s\"]+)",
    "status": r"(?P<status>\d{3})",
    "body_bytes_sent": r"(?P<body_bytes_sent>\d+|-)",
    "bytes_sent": r"(?P<bytes_sent>\d+|-)",
    "http_referer": r"(?P<http_referer>[^\"]*)",
    "http_user_agent": r"(?P<http_user_agent>[^\"]*)",
    "request_time": r"(?P<request_time>[\d.]+)",
}


class LogFormatError(ValueError):
    pass


@dataclass(frozen=True)
class CompiledLogFormat:
    source: str
    pattern: Pattern[str]

    @property
    def has_host(self) -> bool:
        return "host" in self.pattern.groupindex


def expand_format(value: str) -> str:
    normalized = value.strip()
    if normalized == "combined":
        return COMBINED
    if normalized in {"combined_host", "host_combined", "combined with $host prefix"}:
        return COMBINED_HOST
    return normalized


def compile_log_format(value: str) -> CompiledLogFormat:
    source = expand_format(value)
    pieces = []
    position = 0
    seen = set()
    token_re = re.compile(r"\$\{?([A-Za-z0-9_]+)\}?")
    for match in token_re.finditer(source):
        pieces.append(re.escape(source[position : match.start()]))
        name = match.group(1)
        if name in seen:
            raise LogFormatError(f"Log variable ${name} appears more than once")
        field_pattern = FIELD_PATTERNS.get(name)
        if field_pattern is None:
            pieces.append(r"[^\s\"]+" if not _inside_quotes(source, match.start()) else r"[^\"]*")
        else:
            pieces.append(field_pattern)
            seen.add(name)
        position = match.end()
    pieces.append(re.escape(source[position:]))
    required = {"remote_addr", "time_local", "status", "http_user_agent"}
    if not required.issubset(seen):
        missing = ", ".join(f"${name}" for name in sorted(required - seen))
        raise LogFormatError(f"Log format is missing required fields: {missing}")
    if "request" not in seen and not {"request_method", "request_uri"}.issubset(seen):
        raise LogFormatError("Log format needs $request or method and URI fields")
    return CompiledLogFormat(source=source, pattern=re.compile("^" + "".join(pieces) + "$"))


def _inside_quotes(source: str, position: int) -> bool:
    return source[:position].count('"') % 2 == 1

