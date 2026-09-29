"""Small, read-only robots.txt observer for configured sites."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import re
import time
from typing import Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True)
class RobotsRule:
    allow: bool
    path: str


@dataclass(frozen=True)
class RobotsGroup:
    agents: tuple[str, ...]
    rules: tuple[RobotsRule, ...]


_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_SECONDS = 15 * 60
_MAX_BYTES = 512 * 1024


class _SameSiteRedirect(HTTPRedirectHandler):
    """Follow only HTTPS redirects between an apex host and its www alias."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        before = urlsplit(req.full_url)
        after = urlsplit(newurl)
        before_host = (before.hostname or "").removeprefix("www.")
        after_host = (after.hostname or "").removeprefix("www.")
        if after.scheme != "https" or before_host != after_host:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def parse_robots(text: str) -> tuple[RobotsGroup, ...]:
    """Parse the user-agent/allow/disallow subset needed by this dashboard."""
    groups: list[RobotsGroup] = []
    agents: list[str] = []
    rules: list[RobotsRule] = []
    rules_started = False

    def finish() -> None:
        nonlocal agents, rules, rules_started
        if agents:
            groups.append(RobotsGroup(tuple(agents), tuple(rules)))
        agents, rules, rules_started = [], [], False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if rules_started:
                finish()
            agents.append(value.lower())
        elif field in {"allow", "disallow"} and agents:
            rules_started = True
            # An empty Disallow means "disallow nothing" and can be ignored.
            if value:
                rules.append(RobotsRule(field == "allow", value))
    finish()
    return tuple(groups)


def policy_for(groups: Iterable[RobotsGroup], agent: str) -> tuple[str, tuple[RobotsRule, ...]]:
    """Return the matched policy source and combined rules for an agent."""
    lowered = agent.lower()
    matches: list[tuple[int, str, tuple[RobotsRule, ...]]] = []
    for group in groups:
        for token in group.agents:
            if token == "*" or token in lowered:
                matches.append((0 if token == "*" else len(token), token, group.rules))
    if not matches:
        return "none", ()
    specificity = max(item[0] for item in matches)
    selected = [item for item in matches if item[0] == specificity]
    source = "wildcard" if specificity == 0 else "explicit"
    return source, tuple(rule for _, _, rules in selected for rule in rules)


def path_allowed(rules: Iterable[RobotsRule], path: str) -> bool:
    """Apply longest-match semantics, including ``*`` and terminal ``$``."""
    def matches(rule: RobotsRule) -> bool:
        pattern = re.escape(rule.path).replace(r"\*", ".*")
        if pattern.endswith(r"\$"):
            pattern = pattern[:-2] + "$"
        return re.match(pattern, path) is not None

    matched_rules = [rule for rule in rules if matches(rule)]
    if not matched_rules:
        return True
    longest = max(
        len(rule.path.replace("*", "").rstrip("$")) for rule in matched_rules
    )
    return any(
        rule.allow for rule in matched_rules
        if len(rule.path.replace("*", "").rstrip("$")) == longest
    )


def _download(site: str, timeout: float) -> dict:
    url = f"https://{site}/robots.txt"
    request = Request(url, headers={"User-Agent": "Webstats-Policy-Observer/1.0"})
    try:
        with build_opener(_SameSiteRedirect()).open(request, timeout=timeout) as response:
            body = response.read(_MAX_BYTES + 1)
            if len(body) > _MAX_BYTES:
                return {"site": site, "url": url, "status": "too_large", "text": ""}
            charset = response.headers.get_content_charset() or "utf-8"
            return {
                "site": site,
                "url": response.geturl(),
                "status": "available",
                "text": body.decode(charset, errors="replace"),
            }
    except HTTPError as error:
        if error.code == 404:
            return {"site": site, "url": url, "status": "not_found", "text": ""}
        return {
            "site": site, "url": url, "status": "http_error",
            "http_status": error.code, "text": "",
        }
    except (OSError, URLError, TimeoutError) as error:
        return {
            "site": site, "url": url, "status": "unavailable",
            "detail": type(error).__name__, "text": "",
        }


def fetch_robots_policies(
    sites: Iterable[str], timeout: float = 2.5, now: Optional[float] = None
) -> dict[str, dict]:
    """Fetch configured sites concurrently and retain results briefly in memory."""
    timestamp = time.time() if now is None else now
    names = list(dict.fromkeys(sites))
    results: dict[str, dict] = {}
    missing = []
    for site in names:
        cached = _CACHE.get(site)
        if cached and timestamp - cached[0] < _CACHE_SECONDS:
            results[site] = cached[1]
        else:
            missing.append(site)
    if missing:
        with ThreadPoolExecutor(max_workers=min(6, len(missing))) as executor:
            downloaded = executor.map(lambda name: _download(name, timeout), missing)
            for item in downloaded:
                _CACHE[item["site"]] = (timestamp, item)
                results[item["site"]] = item
    return {site: results[site] for site in names}
