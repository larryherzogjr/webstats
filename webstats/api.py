"""JSON API for dashboard pages."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, jsonify, request

from .auth import login_required
from .bots import AI_AGENTS
from .db import connect
from .episodes import detect_episodes
from .privacy import INDIVIDUAL_ACTIVITY_PRIVATE_SITES
from .robots import fetch_robots_policies, parse_robots, path_allowed, policy_for
from .rollup import PROBE_PATH_PREFIXES, PROBE_PATHS
from .sitemaps import fetch_sitemap_inventories


api_bp = Blueprint("api", __name__, url_prefix="/api")

# This application's access log intentionally omits referrers and may contain
# sensitive reading activity. It can contribute anonymous totals, but never
# individual Live Radar rows or country pulses.
LIVE_PRIVATE_SITES = INDIVIDUAL_ACTIVITY_PRIVATE_SITES
SEARCH_CRAWLERS = {
    "Googlebot": ("Google", "Search indexing"),
    "Bingbot": ("Microsoft", "Search indexing"),
    "DuckDuckBot": ("DuckDuckGo", "Search indexing"),
    "Applebot": ("Apple", "Search and assistant indexing"),
}


def _conn() -> sqlite3.Connection:
    config = current_app.config["WEBSTATS_CONFIG"]
    return connect(config.storage.db_path, readonly=True)


def _flags() -> tuple[int, int]:
    return (int(request.args.get("bots", "0") == "1"), int(request.args.get("assets", "0") == "1"))


def _int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        raise ApiError(f"{name} must be an integer")
    return min(max(value, minimum), maximum)


def _path_arg() -> str:
    value = request.args.get("path", "")
    if not value or len(value) > 2048 or not value.startswith("/"):
        raise ApiError("path must be an absolute request path", 400)
    return value


def _date_range() -> tuple[str, str]:
    config = current_app.config["WEBSTATS_CONFIG"]
    today = datetime.now(ZoneInfo(config.server.timezone)).date()
    default_start = today - timedelta(days=6)
    try:
        start = date.fromisoformat(request.args.get("from", default_start.isoformat()))
        end = date.fromisoformat(request.args.get("to", today.isoformat()))
    except ValueError:
        raise ApiError("Dates must use YYYY-MM-DD", 400)
    if start > end:
        raise ApiError("from must not be later than to", 400)
    if (end - start).days > 3660:
        raise ApiError("Date range is too large", 400)
    return start.isoformat(), end.isoformat()


def _year_arg() -> int:
    config = current_app.config["WEBSTATS_CONFIG"]
    current_year = datetime.now(ZoneInfo(config.server.timezone)).year
    try:
        year = int(request.args.get("year", current_year))
    except (TypeError, ValueError):
        raise ApiError("year must be an integer")
    if year < 2000 or year > current_year:
        raise ApiError(f"year must be between 2000 and {current_year}")
    return year


def _day_buckets(start: str, end: str) -> list[str]:
    current = date.fromisoformat(start)
    final = date.fromisoformat(end)
    buckets = []
    while current <= final:
        buckets.append(current.isoformat())
        current += timedelta(days=1)
    return buckets


def _hour_buckets(start: str, end: str, zone: ZoneInfo) -> list[str]:
    first = datetime.combine(date.fromisoformat(start), datetime.min.time(), tzinfo=zone)
    last = datetime.combine(
        date.fromisoformat(end) + timedelta(days=1), datetime.min.time(), tzinfo=zone
    )
    current = first.astimezone(timezone.utc)
    stop = last.astimezone(timezone.utc)
    buckets = []
    while current < stop:
        buckets.append(current.astimezone(zone).strftime("%Y-%m-%dT%H:00:00%z"))
        current += timedelta(hours=1)
    return buckets


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


@api_bp.app_errorhandler(ApiError)
def api_error(error: ApiError):
    return jsonify({"error": error.message}), error.status


def _site(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT id, name FROM sites WHERE name=?", (name,)).fetchone()
    if row is None:
        raise ApiError("Unknown site", 404)
    return row


def _requested_site(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    name = request.args.get("site", "").strip()
    return _site(conn, name) if name else None


def _metric_column(bots: int, assets: int) -> str:
    if bots and assets:
        return "requests"
    if bots:
        return "nonasset_requests"
    if assets:
        return "human_requests"
    return "human_nonasset_requests"


def _is_probe_path(path: str) -> bool:
    lowered = path.lower()
    return lowered in PROBE_PATHS or any(
        lowered.startswith(prefix) for prefix in PROBE_PATH_PREFIXES
    )


def _edit_distance(left: str, right: str) -> int:
    """Return a bounded-cost Levenshtein distance for short request paths."""
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for row_number, right_char in enumerate(right, 1):
        current = [row_number]
        for column, left_char in enumerate(left, 1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def _path_grams(path: str) -> set[str]:
    normalized = path.lower().rstrip("/") or "/"
    if len(normalized) < 3:
        return {normalized}
    return {normalized[index:index + 3] for index in range(len(normalized) - 2)}


def _typo_candidate(
    path: str, successful_paths: Iterable[tuple[str, int]]
) -> Optional[dict[str, Any]]:
    missing = path.lower().rstrip("/") or "/"
    if missing == "/" or len(missing) > 160:
        return None
    best: Optional[dict[str, Any]] = None
    for candidate, successes in successful_paths:
        normalized = candidate.lower().rstrip("/") or "/"
        if normalized in {missing, "/"} or len(normalized) > 160:
            continue
        longest = max(len(missing), len(normalized))
        if longest < 5:
            continue
        allowed = 1 if longest < 8 else 2
        if abs(len(missing) - len(normalized)) > allowed:
            continue
        similarity = SequenceMatcher(None, missing, normalized).ratio()
        if similarity < 0.72:
            continue
        distance = _edit_distance(missing, normalized)
        if not (distance <= allowed or similarity >= 0.88):
            continue
        score = similarity + min(successes, 1000) / 100_000 - distance / 100
        if best is None or score > best["rank"]:
            best = {
                "path": candidate,
                "similarity": round(similarity * 100),
                "rank": score,
            }
    if best:
        best.pop("rank")
    return best


@api_bp.get("/health")
def health():
    config = current_app.config["WEBSTATS_CONFIG"]
    db_path = config.storage.db_path
    state = {}
    try:
        state = json.loads(config.storage.state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    last_run = None
    site_count = 0
    request_count = 0
    try:
        with _conn() as conn:
            row = conn.execute(
                """
                SELECT started_at, finished_at, lines_seen, parsed, parse_failures,
                       inserted, status FROM ingest_runs ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            last_run = dict(row) if row else None
            site_count = conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
            request_count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    except sqlite3.Error:
        pass
    logs = [
        {
            "id": f"log-{index + 1}",
            "offset": int(value.get("offset", 0)),
            "last_run": value.get("last_run"),
        }
        for index, value in enumerate(state.values())
        if isinstance(value, dict)
    ]
    now = int(datetime.now(timezone.utc).timestamp())
    ok = bool(
        last_run
        and last_run.get("status") == "ok"
        and last_run.get("finished_at")
        and last_run["finished_at"] >= now - 15 * 60
    )
    return jsonify(
        {
            "status": "ok" if ok else "degraded",
            "last_ingest": last_run,
            "database_bytes": db_path.stat().st_size if db_path.exists() else 0,
            "sites": site_count,
            "raw_requests": request_count,
            "logs": logs,
        }
    ), (200 if ok else 503)


@api_bp.get("/sites")
@login_required
def sites():
    with _conn() as conn:
        rows = [dict(row) for row in conn.execute("SELECT name FROM sites ORDER BY name")]
    return jsonify({"sites": rows})


@api_bp.get("/events")
@login_required
def events():
    start, end = _date_range()
    limit = _int_arg("limit", 20, 1, 100)
    requested_site = request.args.get("site", "").strip()
    with _conn() as conn:
        site = _site(conn, requested_site) if requested_site else None
        site_clause = "AND e.site_id=?" if site else ""
        parameters: tuple[Any, ...] = (start, end)
        if site:
            parameters += (site["id"],)
        parameters += (limit,)
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT e.kind, e.occurred_at, e.day, e.path, e.source,
                       e.agent, e.country, e.value, s.name site
                FROM events e JOIN sites s ON s.id=e.site_id
                WHERE e.day BETWEEN ? AND ? {site_clause}
                ORDER BY e.occurred_at DESC, e.id DESC LIMIT ?
                """,
                parameters,
            )
        ]
    return jsonify({
        "from": start,
        "to": end,
        "site": requested_site or None,
        "events": rows,
    })


INBOX_CATEGORIES = {
    "discovery": {"new_page", "new_referrer", "content_resurfaced"},
    "readers": {"first_ai_visit", "feed_subscriber_milestone"},
    "momentum": {"traffic_record", "traffic_spike", "visitor_milestone"},
}


@api_bp.get("/inbox")
@login_required
def inbox():
    start, end = _date_range()
    limit = _int_arg("limit", 250, 1, 500)
    category = request.args.get("category", "").strip().lower()
    if category and category not in INBOX_CATEGORIES:
        raise ApiError("Unknown inbox category")
    generated_at = int(datetime.now(timezone.utc).timestamp())
    private_placeholders = ",".join(
        "?" for _ in INDIVIDUAL_ACTIVITY_PRIVATE_SITES
    )
    with _conn() as conn:
        site = _requested_site(conn)
        if site and site["name"] in INDIVIDUAL_ACTIVITY_PRIVATE_SITES:
            return jsonify({
                "scope": {"site": site["name"], "category": category or None},
                "from": start, "to": end, "generated_at": generated_at,
                "privacy": {
                    "protected": True,
                    "excluded_sites": list(INDIVIDUAL_ACTIVITY_PRIVATE_SITES),
                },
                "counts": {"all": 0, **{
                    name: 0 for name in INBOX_CATEGORIES
                }},
                "events": [], "limited": False,
            })
        site_clause = "AND e.site_id=?" if site else ""
        parameters: tuple[Any, ...] = (
            start, end, *INDIVIDUAL_ACTIVITY_PRIVATE_SITES,
            *((site["id"],) if site else ()),
        )
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT e.event_key key, e.kind, e.occurred_at, e.day,
                       e.path, e.source, e.agent, e.country, e.value,
                       s.name site
                FROM events e JOIN sites s ON s.id=e.site_id
                WHERE e.day BETWEEN ? AND ?
                  AND s.name NOT IN ({private_placeholders}) {site_clause}
                ORDER BY e.occurred_at DESC, e.id DESC
                """,
                parameters,
            )
        ]
    for row in rows:
        row["category"] = next(
            (name for name, kinds in INBOX_CATEGORIES.items()
             if row["kind"] in kinds),
            "other",
        )
    counts = {name: 0 for name in INBOX_CATEGORIES}
    for row in rows:
        if row["category"] in counts:
            counts[row["category"]] += 1
    selected = [
        row for row in rows if not category or row["category"] == category
    ]
    return jsonify({
        "scope": {"site": site["name"] if site else None,
                  "category": category or None},
        "from": start, "to": end, "generated_at": generated_at,
        "privacy": {
            "protected": False,
            "excluded_sites": list(INDIVIDUAL_ACTIVITY_PRIVATE_SITES),
        },
        "counts": {"all": len(rows), **counts},
        "events": selected[:limit],
        "limited": len(selected) > limit,
    })


@api_bp.get("/ai-crawlers")
@login_required
def ai_crawlers():
    start, end = _date_range()
    _, assets = _flags()
    limit = _int_arg("limit", 100, 1, 500)
    value = "requests" if assets else "requests-asset_requests"
    families = tuple(AI_AGENTS)
    placeholders = ",".join("?" for _ in families)
    with _conn() as conn:
        site = _requested_site(conn)
        site_clause = "AND site_id=?" if site else ""
        joined_site_clause = "AND p.site_id=?" if site else ""
        totals_params: tuple[Any, ...] = (start, end, *families)
        stored_params: tuple[Any, ...] = (start, end, *families)
        if site:
            totals_params += (site["id"],)
            stored_params += (site["id"],)
        totals_row = conn.execute(
            f"""
            SELECT COALESCE(SUM({value}),0) requests,
                   COUNT(DISTINCT ua_family) agents,
                   COUNT(DISTINCT site_id || char(0) || path) pages,
                   COUNT(DISTINCT site_id) sites,
                   COUNT(DISTINCT site_id || char(0) || ua_family || char(0) || path)
                       sightings
            FROM daily_page_agent
            WHERE day BETWEEN ? AND ? AND ua_family IN ({placeholders})
              AND is_bot=1 AND {value} > 0 {site_clause}
            """,
            totals_params,
        ).fetchone()
        stored = conn.execute(
            f"""
            SELECT s.name site, p.ua_family agent, p.path,
                   SUM({value}) requests, MIN(p.day) first_seen,
                   MAX(p.day) last_seen
            FROM daily_page_agent p JOIN sites s ON s.id=p.site_id
            WHERE p.day BETWEEN ? AND ? AND p.is_bot=1
              AND p.ua_family IN ({placeholders})
              {joined_site_clause}
            GROUP BY p.site_id, p.ua_family, p.path
            HAVING SUM({value}) > 0
            ORDER BY requests DESC, last_seen DESC, site, agent, path
            LIMIT ?
            """,
            (*stored_params, limit),
        )
        rows = []
        for row in stored:
            provider, purpose = AI_AGENTS[row["agent"]]
            rows.append({**dict(row), "provider": provider, "purpose": purpose})
    totals = {
        key: totals_row[key] for key in ("requests", "agents", "pages", "sites")
    }
    return jsonify({
        "from": start,
        "to": end,
        "sightings": rows,
        "totals": totals,
        "limited": totals_row["sightings"] > len(rows),
        "scope": {"site": site["name"] if site else None},
    })


def _changed_rows(
    current: dict[tuple[Any, ...], int], previous: dict[tuple[Any, ...], int],
    fields: tuple[str, ...], limit: int = 12,
) -> list[dict[str, Any]]:
    rows = []
    for key in current.keys() | previous.keys():
        now = current.get(key, 0)
        before = previous.get(key, 0)
        change = now - before
        if not change:
            continue
        if not before:
            category = "new"
        elif not now:
            category = "vanished"
        elif change > 0:
            category = "rising"
        else:
            category = "falling"
        rows.append({
            **dict(zip(fields, key)),
            "requests": now,
            "previous_requests": before,
            "change": change,
            "change_percent": _percent_change(now, before),
            "category": category,
        })
    rows.sort(key=lambda row: (
        -abs(row["change"]), -row["requests"],
        *(str(row[field]) for field in fields),
    ))
    return rows[:limit]


def _change_narrative(
    summary: dict[str, Any], pages: list[dict[str, Any]],
    referrers: list[dict[str, Any]], audience: dict[str, Any],
    errors: dict[str, Any],
) -> list[str]:
    current = summary["requests"]
    previous = summary["previous_requests"]
    percent = summary["change_percent"]
    if not previous:
        lines = [
            f"This window recorded {current:,} human page requests; the preceding "
            "window has no comparable traffic."
        ]
    elif current == previous:
        lines = [f"Human page traffic held steady at {current:,} requests."]
    else:
        direction = "rose" if current > previous else "fell"
        lines = [
            f"Human page traffic {direction} {abs(percent):g}% to {current:,} "
            f"requests, from {previous:,} in the preceding equal-length window."
        ]
    if pages:
        leader = pages[0]
        verb = "added" if leader["change"] > 0 else "lost"
        lines.append(
            f"The largest page movement was {leader['site']}{leader['path']}, "
            f"which {verb} {abs(leader['change']):,} requests."
        )
    positive_sources = [row for row in referrers if row["change"] > 0]
    if positive_sources:
        source = positive_sources[0]
        lines.append(
            f"{source['source']} was the strongest referral change, sending "
            f"{source['requests']:,} visits ({source['change']:+,} versus before)."
        )
    ai_change = audience["ai"]["change"]
    bot_change = audience["bots"]["change"]
    if ai_change:
        direction = "increased" if ai_change > 0 else "decreased"
        lines.append(
            f"Recognized AI crawler activity {direction} by {abs(ai_change):,} "
            f"requests; all bot traffic changed by {bot_change:+,}."
        )
    if errors["change"]:
        direction = "increased" if errors["change"] > 0 else "improved"
        lines.append(
            f"Human 4xx/5xx responses {direction} by {abs(errors['change']):,} "
            f"to {errors['requests']:,}."
        )
    return lines


@api_bp.get("/changes")
@login_required
def changes():
    start_text, end_text = _date_range()
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(end_text)
    span = (end - start).days + 1
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=span - 1)
    previous_start_text = previous_start.isoformat()
    previous_end_text = previous_end.isoformat()
    limit = _int_arg("limit", 12, 1, 50)
    private_placeholders = ",".join("?" for _ in INDIVIDUAL_ACTIVITY_PRIVATE_SITES)

    with _conn() as conn:
        site = _requested_site(conn)
        site_id = site["id"] if site else None
        site_clause = "AND site_id=?" if site else ""
        total_parameters: tuple[Any, ...] = (
            start_text, end_text, start_text, end_text,
            previous_start_text, previous_end_text,
            previous_start_text, previous_end_text,
            start_text, end_text, previous_start_text, previous_end_text,
            start_text, end_text, previous_start_text, previous_end_text,
            *((site_id,) if site else ()),
        )
        totals = dict(conn.execute(
            f"""
            SELECT
              COALESCE(SUM(CASE WHEN day BETWEEN ? AND ? AND include_bots=0
                THEN requests ELSE 0 END),0) requests,
              COALESCE(SUM(CASE WHEN day BETWEEN ? AND ? AND include_bots=0
                THEN unique_visitors ELSE 0 END),0) visitor_days,
              COALESCE(SUM(CASE WHEN day BETWEEN ? AND ? AND include_bots=0
                THEN requests ELSE 0 END),0) previous_requests,
              COALESCE(SUM(CASE WHEN day BETWEEN ? AND ? AND include_bots=0
                THEN unique_visitors ELSE 0 END),0) previous_visitor_days,
              COALESCE(SUM(CASE WHEN day BETWEEN ? AND ? AND include_bots=1
                THEN requests ELSE 0 END),0) all_requests,
              COALESCE(SUM(CASE WHEN day BETWEEN ? AND ? AND include_bots=1
                THEN requests ELSE 0 END),0) previous_all_requests,
              COALESCE(SUM(CASE WHEN day BETWEEN ? AND ? AND include_bots=0
                THEN status_4xx+status_5xx ELSE 0 END),0) errors,
              COALESCE(SUM(CASE WHEN day BETWEEN ? AND ? AND include_bots=0
                THEN status_4xx+status_5xx ELSE 0 END),0) previous_errors
            FROM daily_filter
            WHERE include_assets=0 {site_clause}
            """,
            total_parameters,
        ).fetchone())

        dimensions_allowed = not (
            site and site["name"] in INDIVIDUAL_ACTIVITY_PRIVATE_SITES
        )
        joined_site_clause = "AND x.site_id=?" if site else ""
        common_parameters: tuple[Any, ...] = (
            previous_start_text, end_text, *INDIVIDUAL_ACTIVITY_PRIVATE_SITES,
            *((site_id,) if site else ()),
        )

        def dimensions(table: str, columns: str, group: str, where: str = ""):
            if not dimensions_allowed:
                return []
            return list(conn.execute(
                f"""
                SELECT s.name site, {columns},
                  SUM(CASE WHEN x.day BETWEEN '{start_text}' AND '{end_text}'
                    THEN x.human_nonasset_requests ELSE 0 END) requests,
                  SUM(CASE WHEN x.day BETWEEN '{previous_start_text}' AND '{previous_end_text}'
                    THEN x.human_nonasset_requests ELSE 0 END) previous_requests
                FROM {table} x JOIN sites s ON s.id=x.site_id
                WHERE x.day BETWEEN ? AND ? {where}
                  AND s.name NOT IN ({private_placeholders}) {joined_site_clause}
                GROUP BY x.site_id, {group}
                """,
                common_parameters,
            ))

        def row_maps(rows, keys):
            current, previous = {}, {}
            for row in rows:
                key = tuple(row[name] for name in keys)
                current[key] = row["requests"] or 0
                previous[key] = row["previous_requests"] or 0
            return current, previous

        page_rows = dimensions(
            "daily_page_status", "x.path", "x.path",
            "AND x.status BETWEEN 200 AND 399",
        )
        page_current, page_previous = row_maps(page_rows, ("site", "path"))
        page_changes = _changed_rows(
            page_current, page_previous, ("site", "path"), limit
        )

        referrer_rows = dimensions(
            "daily_page_referrer", "LOWER(x.referrer_host) source",
            "LOWER(x.referrer_host)",
        )
        ref_current, ref_previous = row_maps(referrer_rows, ("site", "source"))
        referrer_changes = _changed_rows(
            ref_current, ref_previous, ("site", "source"), limit
        )

        country_rows = dimensions("daily_country", "x.country", "x.country")
        country_current, country_previous = row_maps(country_rows, ("site", "country"))
        country_changes = _changed_rows(
            country_current, country_previous, ("site", "country"), limit
        )

        error_rows = dimensions(
            "daily_page_status", "x.path", "x.path", "AND x.status>=400"
        )
        error_current, error_previous = row_maps(error_rows, ("site", "path"))
        error_changes = _changed_rows(
            error_current, error_previous, ("site", "path"), limit
        )

        families = tuple(AI_AGENTS)
        family_placeholders = ",".join("?" for _ in families)
        agent_site_clause = "AND x.site_id=?" if site else ""
        agent_parameters: tuple[Any, ...] = (
            previous_start_text, end_text, *families,
            *INDIVIDUAL_ACTIVITY_PRIVATE_SITES, *((site_id,) if site else ()),
        )
        agent_rows = [] if not dimensions_allowed else list(conn.execute(
            f"""
            SELECT s.name site, x.ua_family agent,
              SUM(CASE WHEN x.day BETWEEN ? AND ?
                THEN x.requests-x.asset_requests ELSE 0 END) requests,
              SUM(CASE WHEN x.day BETWEEN ? AND ?
                THEN x.requests-x.asset_requests ELSE 0 END) previous_requests
            FROM daily_page_agent x JOIN sites s ON s.id=x.site_id
            WHERE x.day BETWEEN ? AND ? AND x.is_bot=1
              AND x.ua_family IN ({family_placeholders})
              AND s.name NOT IN ({private_placeholders}) {agent_site_clause}
            GROUP BY x.site_id, x.ua_family
            """,
            (
                start_text, end_text, previous_start_text, previous_end_text,
                *agent_parameters,
            ),
        ))
        ai_requests = sum(row["requests"] or 0 for row in agent_rows)
        ai_previous = sum(row["previous_requests"] or 0 for row in agent_rows)
        agent_current, agent_previous = row_maps(agent_rows, ("site", "agent"))
        agent_changes = _changed_rows(
            agent_current, agent_previous, ("site", "agent"), limit
        )

        feed_site_clause = "AND x.site_id=?" if site else ""
        feed_rows = [] if not dimensions_allowed else list(conn.execute(
            f"""
            SELECT s.name site, x.reader,
              SUM(CASE WHEN x.day BETWEEN ? AND ? THEN x.requests ELSE 0 END) requests,
              SUM(CASE WHEN x.day BETWEEN ? AND ? THEN x.requests ELSE 0 END) previous_requests
            FROM daily_feed_reader x JOIN sites s ON s.id=x.site_id
            WHERE x.day BETWEEN ? AND ?
              AND s.name NOT IN ({private_placeholders}) {feed_site_clause}
            GROUP BY x.site_id, x.reader
            """,
            (
                start_text, end_text, previous_start_text, previous_end_text,
                previous_start_text, end_text, *INDIVIDUAL_ACTIVITY_PRIVATE_SITES,
                *((site_id,) if site else ()),
            ),
        ))
        feed_requests = sum(row["requests"] or 0 for row in feed_rows)
        feed_previous = sum(row["previous_requests"] or 0 for row in feed_rows)

    summary = {
        "requests": totals["requests"],
        "previous_requests": totals["previous_requests"],
        "change": totals["requests"] - totals["previous_requests"],
        "change_percent": _percent_change(
            totals["requests"], totals["previous_requests"]
        ),
        "visitor_days": totals["visitor_days"],
        "previous_visitor_days": totals["previous_visitor_days"],
        "visitor_change": totals["visitor_days"] - totals["previous_visitor_days"],
    }
    bots = totals["all_requests"] - totals["requests"]
    previous_bots = totals["previous_all_requests"] - totals["previous_requests"]
    audience = {
        "bots": {
            "requests": bots, "previous_requests": previous_bots,
            "change": bots - previous_bots,
        },
        "ai": {
            "requests": ai_requests, "previous_requests": ai_previous,
            "change": ai_requests - ai_previous,
        },
        "feeds": {
            "requests": feed_requests, "previous_requests": feed_previous,
            "change": feed_requests - feed_previous,
        },
        "agents": agent_changes,
    }
    errors = {
        "requests": totals["errors"],
        "previous_requests": totals["previous_errors"],
        "change": totals["errors"] - totals["previous_errors"],
        "paths": error_changes,
    }
    return jsonify({
        "window": {
            "from": start_text, "to": end_text, "days": span,
            "previous_from": previous_start_text,
            "previous_to": previous_end_text,
        },
        "scope": {"site": site["name"] if site else None},
        "privacy": {
            "protected": not dimensions_allowed,
            "excluded_sites": list(INDIVIDUAL_ACTIVITY_PRIVATE_SITES),
        },
        "summary": summary,
        "narrative": _change_narrative(
            summary, page_changes, referrer_changes, audience, errors
        ),
        "pages": page_changes,
        "referrers": referrer_changes,
        "countries": country_changes,
        "audience": audience,
        "errors": errors,
    })


@api_bp.get("/ai-policy")
@login_required
def ai_policy():
    start, end = _date_range()
    with _conn() as conn:
        site = _requested_site(conn)
        site_names = [site["name"]] if site else [
            row["name"] for row in conn.execute("SELECT name FROM sites ORDER BY name")
        ]
        visible_names = [
            name for name in site_names
            if name not in INDIVIDUAL_ACTIVITY_PRIVATE_SITES
        ]
        observations: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        if visible_names:
            site_placeholders = ",".join("?" for _ in visible_names)
            family_placeholders = ",".join("?" for _ in AI_AGENTS)
            for row in conn.execute(
                f"""
                SELECT s.name site, x.ua_family agent, x.path,
                       SUM(x.requests-x.asset_requests) requests
                FROM daily_page_agent x JOIN sites s ON s.id=x.site_id
                WHERE x.day BETWEEN ? AND ? AND x.is_bot=1
                  AND x.ua_family IN ({family_placeholders})
                  AND s.name IN ({site_placeholders})
                GROUP BY x.site_id, x.ua_family, x.path
                HAVING SUM(x.requests-x.asset_requests)>0
                ORDER BY requests DESC, x.path
                """,
                (start, end, *AI_AGENTS, *visible_names),
            ):
                observations[(row["site"], row["agent"])].append(dict(row))

    fetched = fetch_robots_policies(site_names)
    site_rows = []
    agent_rows = []
    for site_name in site_names:
        fetched_policy = fetched[site_name]
        status = fetched_policy["status"]
        groups = parse_robots(fetched_policy.get("text", ""))
        protected = site_name in INDIVIDUAL_ACTIVITY_PRIVATE_SITES
        site_conflicts = 0
        site_observed = 0
        explicit_agents = 0
        restricted_agents = 0
        for agent, (provider, purpose) in AI_AGENTS.items():
            source, rules = policy_for(groups, agent)
            if source == "explicit":
                explicit_agents += 1
            disallows = [rule.path for rule in rules if not rule.allow]
            allows = [rule.path for rule in rules if rule.allow]
            if disallows:
                restricted_agents += 1
            observed_paths = [] if protected else observations.get(
                (site_name, agent), []
            )
            conflicts = [
                {"path": item["path"], "requests": item["requests"]}
                for item in observed_paths if not path_allowed(rules, item["path"])
            ] if status == "available" else []
            observed_requests = sum(item["requests"] for item in observed_paths)
            conflict_requests = sum(item["requests"] for item in conflicts)
            site_observed += observed_requests
            site_conflicts += conflict_requests
            if status != "available":
                policy_label = "open" if status == "not_found" else "unknown"
            elif disallows:
                policy_label = "restricted"
            else:
                policy_label = "open"
            agent_rows.append({
                "site": site_name,
                "agent": agent,
                "provider": provider,
                "purpose": purpose,
                "policy": policy_label,
                "policy_source": source if status == "available" else status,
                "disallow": disallows[:8],
                "allow": allows[:8],
                "observed_requests": observed_requests,
                "observed_paths": len(observed_paths),
                "conflict_requests": conflict_requests,
                "conflicts": conflicts[:8],
                "protected": protected,
            })
        site_rows.append({
            "site": site_name,
            "robots_url": fetched_policy["url"],
            "robots_status": status,
            "http_status": fetched_policy.get("http_status"),
            "explicit_agents": explicit_agents,
            "restricted_agents": restricted_agents,
            "observed_requests": site_observed,
            "conflict_requests": site_conflicts,
            "protected": protected,
        })
    agent_rows.sort(key=lambda row: (
        -row["conflict_requests"], -row["observed_requests"],
        row["site"], row["agent"],
    ))
    observed = sum(row["observed_requests"] for row in site_rows)
    conflicts = sum(row["conflict_requests"] for row in site_rows)
    unavailable = sum(
        row["robots_status"] not in {"available", "not_found"}
        for row in site_rows
    )
    if conflicts:
        narrative = (
            f"Webstats found {conflicts:,} observed AI request"
            f"{'s' if conflicts != 1 else ''} to paths disallowed by the current "
            "robots.txt rules. Treat these as policy conflicts to investigate, not "
            "proof of intent."
        )
    elif observed:
        narrative = (
            f"No policy conflicts were found among {observed:,} observed AI requests "
            "in this window."
        )
    else:
        narrative = "No recognized AI crawler requests were observed in this window."
    return jsonify({
        "from": start,
        "to": end,
        "generated_at": int(datetime.now(timezone.utc).timestamp()),
        "scope": {"site": site["name"] if site else None},
        "privacy": {
            "excluded_sites": list(INDIVIDUAL_ACTIVITY_PRIVATE_SITES),
            "protected": bool(site and site["name"] in INDIVIDUAL_ACTIVITY_PRIVATE_SITES),
        },
        "totals": {
            "sites": len(site_rows),
            "robots_available": sum(
                row["robots_status"] == "available" for row in site_rows
            ),
            "unavailable": unavailable,
            "observed_requests": observed,
            "conflict_requests": conflicts,
            "agents_observed": len({
                row["agent"] for row in agent_rows if row["observed_requests"]
            }),
        },
        "narrative": narrative,
        "sites": site_rows,
        "agents": agent_rows,
    })


def _coverage_percent(seen: int, total: int) -> Optional[float]:
    return round(100 * seen / total, 1) if total else None


def _content_state(page: dict[str, Any]) -> tuple[str, str]:
    humans = page["human_requests"]
    lifetime_humans = page["lifetime_human_requests"]
    crawlers = page["search_requests"] + page["ai_requests"]
    lifetime_crawlers = (
        page["lifetime_search_requests"] + page["lifetime_ai_requests"]
    )
    if lifetime_humans == 0 and lifetime_crawlers == 0:
        return "unseen", "Published, but never observed in the retained rollups."
    if lifetime_humans == 0:
        return "crawler-only", "Crawlers found it, but no successful human visit has been observed."
    if humans == 0:
        return "quiet", "Previously read, but quiet throughout the selected window."
    if crawlers >= max(5, humans * 3):
        return "crawler-heavy", "Crawler requests outnumber human visits by at least three to one."
    return "active", "Receiving human attention in the selected window."


@api_bp.get("/content-observatory")
@login_required
def content_observatory():
    start, end = _date_range()
    limit = _int_arg("limit", 100, 10, 500)
    with _conn() as conn:
        selected_site = _requested_site(conn)
        site_rows = list(conn.execute(
            "SELECT id, name FROM sites "
            + ("WHERE id=? " if selected_site else "")
            + "ORDER BY name",
            ((selected_site["id"],) if selected_site else ()),
        ))
    site_names = [row["name"] for row in site_rows]
    robots = fetch_robots_policies(site_names)
    inventories = fetch_sitemap_inventories(site_names, robots)
    visible_sites = [
        name for name in site_names
        if name not in INDIVIDUAL_ACTIVITY_PRIVATE_SITES
    ]
    human_rows = []
    error_rows = []
    crawler_rows = []
    if visible_sites:
        site_placeholders = ",".join("?" for _ in visible_sites)
        crawler_families = tuple({**SEARCH_CRAWLERS, **AI_AGENTS})
        crawler_placeholders = ",".join("?" for _ in crawler_families)
        with _conn() as conn:
            human_rows = list(conn.execute(
                f"""
                SELECT s.name site, x.path,
                  SUM(CASE WHEN x.day BETWEEN ? AND ?
                    THEN x.human_nonasset_requests ELSE 0 END) human_requests,
                  SUM(x.human_nonasset_requests) lifetime_human_requests,
                  MIN(CASE WHEN x.human_nonasset_requests>0 THEN x.day END) first_seen,
                  MAX(CASE WHEN x.human_nonasset_requests>0 THEN x.day END) last_seen
                FROM daily_page_status x JOIN sites s ON s.id=x.site_id
                WHERE x.status BETWEEN 200 AND 399
                  AND s.name IN ({site_placeholders})
                GROUP BY x.site_id, x.path
                HAVING SUM(x.human_nonasset_requests)>0
                """,
                (start, end, *visible_sites),
            ))
            error_rows = list(conn.execute(
                f"""
                SELECT s.name site, x.path,
                       SUM(x.human_nonasset_requests) error_requests
                FROM daily_page_status x JOIN sites s ON s.id=x.site_id
                WHERE x.day BETWEEN ? AND ? AND x.status>=400
                  AND s.name IN ({site_placeholders})
                GROUP BY x.site_id, x.path
                HAVING SUM(x.human_nonasset_requests)>0
                """,
                (start, end, *visible_sites),
            ))
            crawler_rows = list(conn.execute(
                f"""
                SELECT s.name site, x.path, x.ua_family agent,
                  SUM(CASE WHEN x.day BETWEEN ? AND ?
                    THEN x.requests-x.asset_requests ELSE 0 END) requests,
                  SUM(x.requests-x.asset_requests) lifetime_requests
                FROM daily_page_agent x JOIN sites s ON s.id=x.site_id
                WHERE x.is_bot=1 AND x.ua_family IN ({crawler_placeholders})
                  AND s.name IN ({site_placeholders})
                GROUP BY x.site_id, x.path, x.ua_family
                HAVING SUM(x.requests-x.asset_requests)>0
                """,
                (start, end, *crawler_families, *visible_sites),
            ))

    humans = {}
    for row in human_rows:
        if _is_probe_path(row["path"]):
            continue
        item = dict(row)
        item["human_requests"] = item["human_requests"] or 0
        item["lifetime_human_requests"] = item["lifetime_human_requests"] or 0
        humans[(row["site"], row["path"])] = item
    errors = {
        (row["site"], row["path"]): row["error_requests"]
        for row in error_rows if not _is_probe_path(row["path"])
    }
    crawlers = {
        (row["site"], row["path"], row["agent"]): dict(row)
        for row in crawler_rows if not _is_probe_path(row["path"])
    }
    agents = {**SEARCH_CRAWLERS, **AI_AGENTS}
    pages = []
    conflicts = []
    off_sitemap = []
    crawler_coverage = []
    summaries = []

    for site_name in site_names:
        protected = site_name in INDIVIDUAL_ACTIVITY_PRIVATE_SITES
        inventory = inventories[site_name]
        sitemap_paths = {item["path"] for item in inventory["pages"]}
        robots_status = robots[site_name]["status"]
        groups = parse_robots(robots[site_name].get("text", ""))
        site_pages = []
        if not protected:
            for item in inventory["pages"]:
                path = item["path"]
                human = humans.get((site_name, path), {})
                search_current = search_lifetime = 0
                ai_current = ai_lifetime = 0
                blocked_agents = []
                conflict_requests = 0
                for agent in agents:
                    observed = crawlers.get((site_name, path, agent), {})
                    current_requests = observed.get("requests", 0) or 0
                    lifetime_requests = observed.get("lifetime_requests", 0) or 0
                    if agent in SEARCH_CRAWLERS:
                        search_current += current_requests
                        search_lifetime += lifetime_requests
                    else:
                        ai_current += current_requests
                        ai_lifetime += lifetime_requests
                    if robots_status == "available":
                        _, rules = policy_for(groups, agent)
                        if not path_allowed(rules, path):
                            blocked_agents.append(agent)
                            conflict_requests += current_requests
                page = {
                    **item,
                    "site": site_name,
                    "human_requests": human.get("human_requests", 0) or 0,
                    "lifetime_human_requests": human.get(
                        "lifetime_human_requests", 0
                    ) or 0,
                    "first_seen": human.get("first_seen"),
                    "last_seen": human.get("last_seen"),
                    "search_requests": search_current,
                    "lifetime_search_requests": search_lifetime,
                    "ai_requests": ai_current,
                    "lifetime_ai_requests": ai_lifetime,
                    "error_requests": errors.get((site_name, path), 0),
                    "blocked_agents": blocked_agents,
                    "conflict_requests": conflict_requests,
                }
                page["state"], page["explanation"] = _content_state(page)
                site_pages.append(page)
                pages.append(page)
                if blocked_agents:
                    conflicts.append({
                        "site": site_name, "path": path,
                        "agents": blocked_agents,
                        "observed_requests": conflict_requests,
                    })

            active_paths = [
                row for (name, path), row in humans.items()
                if name == site_name and row["human_requests"] > 0
                and path not in sitemap_paths
            ]
            for row in active_paths:
                off_sitemap.append({
                    "site": site_name,
                    "path": row["path"],
                    "requests": row["human_requests"],
                    "lifetime_requests": row["lifetime_human_requests"],
                    "first_seen": row["first_seen"],
                    "last_seen": row["last_seen"],
                })

            for agent, (provider, purpose) in agents.items():
                agent_rows = [
                    row for (name, _path, family), row in crawlers.items()
                    if name == site_name and family == agent
                ]
                lifetime_paths = {
                    row["path"] for row in agent_rows if row["lifetime_requests"]
                }
                current_paths = {
                    row["path"] for row in agent_rows if row["requests"]
                }
                blocked_paths = {
                    row["path"] for row in site_pages if agent in row["blocked_agents"]
                }
                if not agent_rows and not blocked_paths:
                    continue
                crawler_coverage.append({
                    "site": site_name,
                    "agent": agent,
                    "provider": provider,
                    "purpose": purpose,
                    "kind": "search" if agent in SEARCH_CRAWLERS else "ai",
                    "requests": sum(row["requests"] or 0 for row in agent_rows),
                    "lifetime_requests": sum(
                        row["lifetime_requests"] or 0 for row in agent_rows
                    ),
                    "sitemap_pages_seen": len(lifetime_paths & sitemap_paths),
                    "current_sitemap_pages": len(current_paths & sitemap_paths),
                    "off_sitemap_pages": len(current_paths - sitemap_paths),
                    "blocked_sitemap_pages": len(blocked_paths),
                    "coverage_percent": _coverage_percent(
                        len(lifetime_paths & sitemap_paths), len(sitemap_paths)
                    ),
                })

        human_seen = sum(page["lifetime_human_requests"] > 0 for page in site_pages)
        search_seen = sum(page["lifetime_search_requests"] > 0 for page in site_pages)
        ai_seen = sum(page["lifetime_ai_requests"] > 0 for page in site_pages)
        summaries.append({
            "site": site_name,
            "sitemap_status": inventory["status"],
            "robots_status": robots_status,
            "published_pages": len(inventory["pages"]),
            "sitemap_documents": len(inventory["documents"]),
            "declared_sitemaps": len(inventory["declared"]),
            "discovery_errors": len(inventory["errors"]),
            "limited": inventory["limited"],
            "protected": protected,
            "human_pages_seen": human_seen if not protected else None,
            "human_coverage_percent": (
                _coverage_percent(human_seen, len(site_pages)) if not protected else None
            ),
            "search_coverage_percent": (
                _coverage_percent(search_seen, len(site_pages)) if not protected else None
            ),
            "ai_coverage_percent": (
                _coverage_percent(ai_seen, len(site_pages)) if not protected else None
            ),
            "dark_pages": sum(
                page["state"] in {"unseen", "crawler-only", "quiet", "crawler-heavy"}
                for page in site_pages
            ),
            "off_sitemap_pages": sum(
                row["site"] == site_name for row in off_sitemap
            ),
            "policy_conflicts": sum(
                row["site"] == site_name for row in conflicts
            ),
        })

    dark_priority = {
        "unseen": 0, "crawler-only": 1, "crawler-heavy": 2, "quiet": 3,
        "active": 4,
    }
    dark_matter = [page for page in pages if page["state"] != "active"]
    dark_matter.sort(key=lambda row: (
        dark_priority[row["state"]],
        -(row["search_requests"] + row["ai_requests"]),
        row["site"], row["path"],
    ))
    off_sitemap.sort(key=lambda row: (-row["requests"], row["site"], row["path"]))
    conflicts.sort(key=lambda row: (
        -row["observed_requests"], row["site"], row["path"]
    ))
    crawler_coverage.sort(key=lambda row: (
        0 if row["kind"] == "search" else 1,
        -row["requests"], row["site"], row["agent"],
    ))
    analyzed_pages = len(pages)
    human_seen = sum(page["lifetime_human_requests"] > 0 for page in pages)
    search_seen = sum(page["lifetime_search_requests"] > 0 for page in pages)
    ai_seen = sum(page["lifetime_ai_requests"] > 0 for page in pages)
    published_pages = sum(row["published_pages"] for row in summaries)
    if not published_pages:
        narrative = (
            "No sitemap inventory was discovered. Active successful pages are shown "
            "as inventory drift so you can see what a future sitemap should cover."
        )
    elif not analyzed_pages:
        narrative = (
            f"Webstats discovered {published_pages:,} published pages, but page-level "
            "attention and crawler comparisons are hidden by this site's privacy boundary."
        )
    elif dark_matter:
        narrative = (
            f"Webstats discovered {published_pages:,} published pages and found "
            f"{len(dark_matter):,} with weak or absent human attention. "
            f"{len(off_sitemap):,} active pages sit outside the current sitemap inventory."
        )
    else:
        narrative = (
            f"All {analyzed_pages:,} analyzable sitemap pages have current human "
            "attention; no content dark matter was detected in this window."
        )
    return jsonify({
        "from": start,
        "to": end,
        "generated_at": int(datetime.now(timezone.utc).timestamp()),
        "scope": {"site": selected_site["name"] if selected_site else None},
        "privacy": {
            "protected": bool(
                selected_site
                and selected_site["name"] in INDIVIDUAL_ACTIVITY_PRIVATE_SITES
            ),
            "excluded_sites": list(INDIVIDUAL_ACTIVITY_PRIVATE_SITES),
        },
        "narrative": narrative,
        "totals": {
            "published_pages": published_pages,
            "analyzed_pages": analyzed_pages,
            "dark_pages": len(dark_matter),
            "off_sitemap_pages": len(off_sitemap),
            "policy_conflicts": len(conflicts),
            "human_coverage_percent": _coverage_percent(human_seen, analyzed_pages),
            "search_coverage_percent": _coverage_percent(search_seen, analyzed_pages),
            "ai_coverage_percent": _coverage_percent(ai_seen, analyzed_pages),
        },
        "sites": summaries,
        "dark_matter": dark_matter[:limit],
        "off_sitemap": off_sitemap[:limit],
        "policy_conflicts": conflicts[:limit],
        "crawlers": crawler_coverage,
        "limited": {
            "dark_matter": len(dark_matter) > limit,
            "off_sitemap": len(off_sitemap) > limit,
            "policy_conflicts": len(conflicts) > limit,
        },
    })


def _episode_dimension(
    conn: sqlite3.Connection, table: str, key_select: str, key_name: str,
    metric: str, site_id: int, start: date, end: date,
    previous_start: date, previous_end: date, where: str = "",
    extra_parameters: tuple[Any, ...] = (), limit: int = 6,
) -> list[dict[str, Any]]:
    rows = []
    for row in conn.execute(
        f"""
        SELECT {key_select} value,
          SUM(CASE WHEN x.day BETWEEN ? AND ? THEN {metric} ELSE 0 END) requests,
          SUM(CASE WHEN x.day BETWEEN ? AND ? THEN {metric} ELSE 0 END) previous_requests
        FROM {table} x
        WHERE x.day BETWEEN ? AND ? AND x.site_id=? {where}
        GROUP BY value
        HAVING SUM(CASE WHEN x.day BETWEEN ? AND ? THEN {metric} ELSE 0 END)>0
        """,
        (
            start.isoformat(), end.isoformat(),
            previous_start.isoformat(), previous_end.isoformat(),
            previous_start.isoformat(), end.isoformat(), site_id,
            *extra_parameters, start.isoformat(), end.isoformat(),
        ),
    ):
        item = {
            key_name: row["value"],
            "requests": row["requests"] or 0,
            "previous_requests": row["previous_requests"] or 0,
        }
        item["change"] = item["requests"] - item["previous_requests"]
        item["new"] = item["previous_requests"] == 0
        rows.append(item)
    rows.sort(key=lambda item: (
        -max(item["change"], 0), -item["requests"], str(item[key_name])
    ))
    return rows[:limit]


def _episode_narrative(episode: dict[str, Any]) -> list[str]:
    multiple = episode["peak_multiple"]
    peak_context = (
        f"{multiple:g} times its preceding baseline"
        if multiple is not None else "above a previously quiet baseline"
    )
    lines = [
        f"Attention peaked at {episode['peak_requests']:,} requests on "
        f"{episode['peak_day']}, {peak_context}. The episode produced "
        f"{episode['excess_requests']:,} requests above its expected level."
    ]
    pages = episode["drivers"]["pages"]
    if pages:
        page = pages[0]
        lines.append(
            f"{page['path']} was the largest content driver with "
            f"{page['requests']:,} requests ({page['change']:+,} versus the "
            "preceding equal-length window)."
        )
    referrers = episode["drivers"]["referrers"]
    if referrers:
        source = referrers[0]
        lines.append(
            f"{source['source']} was the strongest referring source, sending "
            f"{source['requests']:,} requests ({source['change']:+,})."
        )
    errors = episode["drivers"]["errors"]
    if errors["requests"]:
        lines.append(
            f"Human-classified errors contributed {errors['requests']:,} requests "
            f"({errors['change']:+,} versus before)."
        )
    crawlers = episode["drivers"]["crawlers"]
    if crawlers:
        leader = crawlers[0]
        lines.append(
            f"In parallel, {leader['agent']} made {leader['requests']:,} page "
            f"requests ({leader['change']:+,})."
        )
    aftermath = episode["lasting_effect"]
    if aftermath == "sustained":
        lines.append("Attention remained materially above baseline in the following week.")
    elif aftermath == "returned":
        lines.append("Traffic returned near its earlier baseline in the following week.")
    else:
        lines.append("The episode is too recent to judge its lasting effect.")
    return lines


@api_bp.get("/episodes")
@login_required
def attention_episodes():
    start_text, end_text = _date_range()
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(end_text)
    limit = _int_arg("limit", 20, 1, 50)
    config = current_app.config["WEBSTATS_CONFIG"]
    today = datetime.now(ZoneInfo(config.server.timezone)).date()
    analysis_start = start - timedelta(days=28)
    analysis_end = min(today, end + timedelta(days=14))
    with _conn() as conn:
        selected_site = _requested_site(conn)
        if (
            selected_site
            and selected_site["name"] in INDIVIDUAL_ACTIVITY_PRIVATE_SITES
        ):
            return jsonify({
                "window": {"from": start_text, "to": end_text},
                "scope": {"site": selected_site["name"]},
                "privacy": {
                    "protected": True,
                    "excluded_sites": list(INDIVIDUAL_ACTIVITY_PRIVATE_SITES),
                },
                "summary": {
                    "episodes": 0, "active": 0, "sites": 0,
                    "excess_requests": 0, "longest_days": 0,
                },
                "narrative": "Attention episodes are intentionally unavailable for this privacy-protected site.",
                "episodes": [],
            })
        private_placeholders = ",".join(
            "?" for _ in INDIVIDUAL_ACTIVITY_PRIVATE_SITES
        )
        site_clause = "AND id=?" if selected_site else ""
        sites = list(conn.execute(
            f"""
            SELECT id, name FROM sites
            WHERE name NOT IN ({private_placeholders}) {site_clause}
            ORDER BY name
            """,
            (*INDIVIDUAL_ACTIVITY_PRIVATE_SITES,
             *((selected_site["id"],) if selected_site else ())),
        ))
        stored = conn.execute(
            f"""
            SELECT f.site_id, f.day, f.requests
            FROM daily_filter f JOIN sites s ON s.id=f.site_id
            WHERE f.day BETWEEN ? AND ? AND f.include_bots=0
              AND f.include_assets=0
              AND s.name NOT IN ({private_placeholders})
              {('AND f.site_id=?' if selected_site else '')}
            """,
            (
                analysis_start.isoformat(), analysis_end.isoformat(),
                *INDIVIDUAL_ACTIVITY_PRIVATE_SITES,
                *((selected_site["id"],) if selected_site else ()),
            ),
        )
        values = {
            (row["site_id"], row["day"]): row["requests"] for row in stored
        }
        buckets = _day_buckets(
            analysis_start.isoformat(), analysis_end.isoformat()
        )
        detected = []
        for site in sites:
            series = [
                {"day": day, "requests": values.get((site["id"], day), 0)}
                for day in buckets
            ]
            for item in detect_episodes(series, start, end):
                item["site"] = site["name"]
                item["site_id"] = site["id"]
                detected.append(item)

        detected.sort(key=lambda item: (
            -item["excess_requests"], item["start"], item["site"]
        ))
        detected = detected[:limit]
        all_agents = tuple({**SEARCH_CRAWLERS, **AI_AGENTS})
        agent_placeholders = ",".join("?" for _ in all_agents)
        for episode in detected:
            episode_start = date.fromisoformat(episode["start"])
            episode_end = date.fromisoformat(episode["end"])
            duration = (episode_end - episode_start).days + 1
            prior_end = episode_start - timedelta(days=1)
            prior_start = prior_end - timedelta(days=duration - 1)
            site_id = episode["site_id"]
            pages = _episode_dimension(
                conn, "daily_page_status", "x.path", "path",
                "x.human_nonasset_requests", site_id,
                episode_start, episode_end, prior_start, prior_end,
                "AND x.status BETWEEN 200 AND 399",
            )
            referrer_rows = _episode_dimension(
                conn, "daily_page_referrer", "LOWER(x.referrer_host)", "source",
                "x.human_nonasset_requests", site_id,
                episode_start, episode_end, prior_start, prior_end,
                limit=1000,
            )
            countries = _episode_dimension(
                conn, "daily_country", "x.country", "country",
                "x.human_nonasset_requests", site_id,
                episode_start, episode_end, prior_start, prior_end,
            )
            crawlers = _episode_dimension(
                conn, "daily_page_agent", "x.ua_family", "agent",
                "x.requests-x.asset_requests", site_id,
                episode_start, episode_end, prior_start, prior_end,
                f"AND x.is_bot=1 AND x.ua_family IN ({agent_placeholders})",
                all_agents, limit=20,
            )
            for item in crawlers:
                provider, purpose = (
                    SEARCH_CRAWLERS.get(item["agent"])
                    or AI_AGENTS[item["agent"]]
                )
                item["provider"] = provider
                item["purpose"] = purpose
                item["kind"] = (
                    "search" if item["agent"] in SEARCH_CRAWLERS else "ai"
                )
            error_paths = _episode_dimension(
                conn, "daily_page_status", "x.path", "path",
                "x.human_nonasset_requests", site_id,
                episode_start, episode_end, prior_start, prior_end,
                "AND x.status>=400", limit=1000,
            )
            error_totals = conn.execute(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN day BETWEEN ? AND ?
                    THEN status_4xx+status_5xx ELSE 0 END),0) requests,
                  COALESCE(SUM(CASE WHEN day BETWEEN ? AND ?
                    THEN status_4xx+status_5xx ELSE 0 END),0) previous_requests
                FROM daily_filter
                WHERE site_id=? AND include_bots=0 AND include_assets=0
                  AND day BETWEEN ? AND ?
                """,
                (
                    episode["start"], episode["end"],
                    prior_start.isoformat(), prior_end.isoformat(), site_id,
                    prior_start.isoformat(), episode["end"],
                ),
            ).fetchone()
            error_requests = error_totals["requests"]
            previous_errors = error_totals["previous_requests"]
            moments = [
                dict(row) for row in conn.execute(
                    """
                    SELECT kind, day, occurred_at, path, source, agent, country, value
                    FROM events WHERE site_id=? AND day BETWEEN ? AND ?
                    ORDER BY occurred_at, id
                    """,
                    (site_id, episode["start"], episode["end"]),
                )
            ]
            referred = sum(item["requests"] for item in referrer_rows)
            episode["drivers"] = {
                "pages": pages,
                "referrers": referrer_rows[:6],
                "countries": countries,
                "crawlers": crawlers,
                "errors": {
                    "requests": error_requests,
                    "previous_requests": previous_errors,
                    "change": error_requests - previous_errors,
                    "paths": error_paths[:6],
                },
                "moments": moments,
                "direct_or_unknown": max(0, episode["requests"] - referred),
            }
            if error_requests >= max(3, episode["requests"] * 0.5):
                episode["kind"] = "error-wave"
            elif (
                referrer_rows and referrer_rows[0]["change"]
                >= max(3, episode["excess_requests"] * 0.5)
            ):
                episode["kind"] = "referral-wave"
            elif (
                pages and pages[0]["new"] and pages[0]["requests"]
                >= max(3, episode["excess_requests"] * 0.5)
            ):
                episode["kind"] = "breakout"
            else:
                episode["kind"] = "attention-wave"
            episode["narrative"] = _episode_narrative(episode)
            episode["id"] = f"{episode['site']}:{episode['start']}"
            episode.pop("site_id", None)

    kinds = defaultdict(int)
    for episode in detected:
        kinds[episode["kind"]] += 1
    active = sum(
        episode["trajectory"] in {"active", "plateau", "fading"}
        and episode["lasting_effect"] == "unresolved"
        for episode in detected
    )
    excess = sum(episode["excess_requests"] for episode in detected)
    if not detected:
        narrative = (
            "No statistically unusual attention episodes were detected in this "
            "window. Ordinary day-to-day movement remains in the Change Engine."
        )
    else:
        largest = detected[0]
        narrative = (
            f"Webstats reconstructed {len(detected)} attention episode"
            f"{'s' if len(detected) != 1 else ''} across "
            f"{len({item['site'] for item in detected})} site"
            f"{'s' if len({item['site'] for item in detected}) != 1 else ''}, "
            f"representing {excess:,} requests above baseline. The largest peaked "
            f"on {largest['peak_day']} at {largest['peak_requests']:,} requests."
        )
    return jsonify({
        "window": {"from": start_text, "to": end_text},
        "scope": {"site": selected_site["name"] if selected_site else None},
        "privacy": {
            "protected": False,
            "excluded_sites": list(INDIVIDUAL_ACTIVITY_PRIVATE_SITES),
        },
        "summary": {
            "episodes": len(detected),
            "active": active,
            "sites": len({item["site"] for item in detected}),
            "excess_requests": excess,
            "longest_days": max(
                (item["duration_days"] for item in detected), default=0
            ),
            "kinds": dict(kinds),
        },
        "narrative": narrative,
        "episodes": detected,
    })


@api_bp.get("/feed-readers")
@login_required
def feed_readers():
    start, end = _date_range()
    limit = _int_arg("limit", 250, 1, 500)
    with _conn() as conn:
        site = _requested_site(conn)
        site_clause = "AND f.site_id=?" if site else ""
        parameters: tuple[Any, ...] = (start, end)
        if site:
            parameters += (site["id"],)
        stored = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT f.day, s.name site, f.path, f.reader, f.requests,
                       f.reported_subscribers, f.first_seen, f.last_seen
                FROM daily_feed_reader f JOIN sites s ON s.id=f.site_id
                WHERE f.day BETWEEN ? AND ? {site_clause}
                ORDER BY f.day, f.last_seen
                """,
                parameters,
            )
        ]

    sightings: dict[tuple[str, str, str], dict[str, Any]] = {}
    daily = {
        day: {"bucket": day, "requests": 0, "reported_subscribers": None}
        for day in _day_buckets(start, end)
    }
    for row in stored:
        key = (row["site"], row["path"], row["reader"])
        item = sightings.setdefault(
            key,
            {
                "site": row["site"],
                "path": row["path"],
                "reader": row["reader"],
                "requests": 0,
                "latest_subscribers": None,
                "first_reported_subscribers": None,
                "peak_subscribers": None,
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
            },
        )
        item["requests"] += row["requests"]
        item["first_seen"] = min(item["first_seen"], row["first_seen"])
        item["last_seen"] = max(item["last_seen"], row["last_seen"])
        count = row["reported_subscribers"]
        if count is not None:
            if item["first_reported_subscribers"] is None:
                item["first_reported_subscribers"] = count
            item["latest_subscribers"] = count
            item["peak_subscribers"] = max(item["peak_subscribers"] or 0, count)
            current = daily[row["day"]]["reported_subscribers"]
            daily[row["day"]]["reported_subscribers"] = (current or 0) + count
        daily[row["day"]]["requests"] += row["requests"]

    rows = sorted(
        sightings.values(),
        key=lambda item: (
            -(item["latest_subscribers"] if item["latest_subscribers"] is not None else -1),
            -item["last_seen"], item["site"], item["path"], item["reader"],
        ),
    )
    latest = sum(
        item["latest_subscribers"] for item in rows
        if item["latest_subscribers"] is not None
    )
    first = sum(
        item["first_reported_subscribers"] for item in rows
        if item["first_reported_subscribers"] is not None
    )
    totals = {
        "reported_subscribers": latest,
        "subscriber_change": latest - first,
        "readers": len({item["reader"] for item in rows}),
        "feeds": len({(item["site"], item["path"]) for item in rows}),
        "requests": sum(item["requests"] for item in rows),
        "reporting_feeds": sum(item["latest_subscribers"] is not None for item in rows),
    }
    return jsonify({
        "from": start,
        "to": end,
        "series": list(daily.values()),
        "sightings": rows[:limit],
        "totals": totals,
        "limited": len(rows) > limit,
        "scope": {"site": site["name"] if site else None},
    })


def _streaks(active: list[date], today: date) -> dict[str, Any]:
    longest = 0
    longest_start = None
    longest_end = None
    run = 0
    run_start = None
    previous = None
    for day in active:
        if previous is not None and day == previous + timedelta(days=1):
            run += 1
        else:
            run = 1
            run_start = day
        if run > longest:
            longest = run
            longest_start = run_start
            longest_end = day
        previous = day

    active_set = set(active)
    current = 0
    # Today is incomplete; a streak through yesterday remains current until
    # the day ends, while any traffic today naturally extends it.
    cursor = today if today in active_set else today - timedelta(days=1)
    while cursor in active_set:
        current += 1
        cursor -= timedelta(days=1)
    return {
        "current": current,
        "longest": longest,
        "longest_start": longest_start.isoformat() if longest_start else None,
        "longest_end": longest_end.isoformat() if longest_end else None,
    }


def _best_period(totals: dict[str, dict[str, int]]) -> Optional[dict[str, Any]]:
    if not totals:
        return None
    period, values = min(
        totals.items(), key=lambda item: (-item[1]["requests"], item[0])
    )
    return {"period": period, **values}


def _percent_change(current: int, previous: int) -> Optional[float]:
    if previous == 0:
        return None
    return round(100 * (current - previous) / previous, 1)


def _week_start_arg(today: date) -> Optional[date]:
    current_week = today - timedelta(days=today.weekday())
    value = request.args.get("week", "").strip()
    if not value:
        return None
    try:
        selected = date.fromisoformat(value)
    except ValueError:
        raise ApiError("week must use YYYY-MM-DD")
    if selected.weekday() != 0:
        raise ApiError("week must be a Monday")
    if selected > current_week:
        raise ApiError("week must not be in the future")
    return selected


def _briefing_totals(
    conn: sqlite3.Connection, start: date, end: date,
    site_id: Optional[int] = None,
) -> dict[str, int]:
    site_clause = "AND site_id=?" if site_id is not None else ""
    parameters: tuple[Any, ...] = (start.isoformat(), end.isoformat())
    if site_id is not None:
        parameters += (site_id,)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(requests),0) requests,
               COALESCE(SUM(unique_visitors),0) visitor_days,
               COALESCE(SUM(bytes),0) bytes,
               COUNT(DISTINCT CASE WHEN requests>0 THEN site_id END) active_sites
        FROM daily_filter
        WHERE day BETWEEN ? AND ? AND include_bots=0 AND include_assets=0
          {site_clause}
        """,
        parameters,
    ).fetchone()
    return dict(row)


def _briefing_page_totals(
    conn: sqlite3.Connection, start: date, end: date,
    site_id: Optional[int] = None,
) -> dict[tuple[str, str], int]:
    site_clause = "AND p.site_id=?" if site_id is not None else ""
    parameters: tuple[Any, ...] = (start.isoformat(), end.isoformat())
    if site_id is not None:
        parameters += (site_id,)
    return {
        (row["site"], row["path"]): row["requests"]
        for row in conn.execute(
            f"""
            SELECT s.name site, p.path,
                   SUM(p.human_nonasset_requests) requests
            FROM daily_page_status p JOIN sites s ON s.id=p.site_id
            WHERE p.day BETWEEN ? AND ? AND p.status BETWEEN 200 AND 399
              {site_clause}
            GROUP BY p.site_id, p.path
            HAVING SUM(p.human_nonasset_requests)>0
            """,
            parameters,
        )
    }


def _feed_snapshot(
    conn: sqlite3.Connection, through: date, site_id: Optional[int] = None
) -> dict[str, int]:
    site_clause = "AND f.site_id=?" if site_id is not None else ""
    parameters: tuple[Any, ...] = (through.isoformat(),)
    if site_id is not None:
        parameters += (site_id,)
    rows = conn.execute(
        f"""
        SELECT s.name site, f.path, f.reader, f.day, f.reported_subscribers
        FROM daily_feed_reader f JOIN sites s ON s.id=f.site_id
        WHERE f.day<=? AND f.reported_subscribers IS NOT NULL
          {site_clause}
        ORDER BY f.day
        """,
        parameters,
    )
    latest: dict[tuple[str, str, str], int] = {}
    for row in rows:
        latest[(row["site"], row["path"], row["reader"])] = row[
            "reported_subscribers"
        ]
    return {
        "reported_subscribers": sum(latest.values()),
        "reporting_feeds": len(latest),
    }


def _briefing_narrative(
    summary: dict[str, Any], pages: list[dict[str, Any]], discoveries: dict[str, Any],
    ai: dict[str, Any], geography: dict[str, Any], feeds: dict[str, Any],
) -> list[str]:
    requests = summary["requests"]
    previous = summary["previous_requests"]
    change = summary["request_change_percent"]
    lines = []
    if previous == 0:
        lines.append(
            f"The week recorded {requests:,} human page request{'s' if requests != 1 else ''}; "
            "there is no comparable traffic in the prior week."
        )
    elif requests == previous:
        lines.append(f"Traffic held steady at {requests:,} human page requests.")
    else:
        direction = "rose" if requests > previous else "fell"
        lines.append(
            f"Human page traffic {direction} {abs(change):g}% to {requests:,} requests, "
            f"from {previous:,} in the comparable prior period."
        )
    gainers = [page for page in pages if page["change"] > 0]
    if gainers:
        leader = max(gainers, key=lambda page: page["change"])
        lines.append(
            f"{leader['site']}{leader['path']} gained the most attention, adding "
            f"{leader['change']:,} requests."
        )
    if discoveries["pages"] or discoveries["referrers"]:
        parts = []
        if discoveries["pages"]:
            parts.append(f"{discoveries['pages']} new page{'s' if discoveries['pages'] != 1 else ''}")
        if discoveries["referrers"]:
            parts.append(
                f"{discoveries['referrers']} new referrer{'s' if discoveries['referrers'] != 1 else ''}"
            )
        lines.append("Webstats discovered " + " and ".join(parts) + ".")
    if discoveries["resurfaced"]:
        lines.append(
            f"{discoveries['resurfaced']} older page"
            f"{'s' if discoveries['resurfaced'] != 1 else ''} resurfaced after a quiet spell."
        )
    if ai["requests"]:
        lines.append(
            f"Recognized AI crawlers made {ai['requests']:,} page requests across "
            f"{ai['pages']:,} page{'s' if ai['pages'] != 1 else ''}."
        )
    if geography["new_countries"]:
        lines.append(
            "First-time traffic arrived from "
            + ", ".join(geography["new_countries"][:5])
            + (" and elsewhere." if len(geography["new_countries"]) > 5 else ".")
        )
    if feeds["change"]:
        direction = "grew" if feeds["change"] > 0 else "declined"
        lines.append(
            f"Reported feed subscriptions {direction} by {abs(feeds['change']):,} "
            f"to {feeds['reported_subscribers']:,}."
        )
    return lines


@api_bp.get("/briefing")
@login_required
def briefing():
    config = current_app.config["WEBSTATS_CONFIG"]
    today = datetime.now(ZoneInfo(config.server.timezone)).date()
    current_week = today - timedelta(days=today.weekday())
    selected = _week_start_arg(today)
    with _conn() as conn:
        site = _requested_site(conn)
        site_id = site["id"] if site else None
        if selected is None:
            site_clause = "AND site_id=?" if site else ""
            latest_completed_day = conn.execute(
                f"""
                SELECT MAX(day) FROM daily_filter
                WHERE include_bots=0 AND include_assets=0 AND requests>0 AND day<?
                  {site_clause}
                """,
                (current_week.isoformat(), *((site_id,) if site else ())),
            ).fetchone()[0]
            # Prefer the newest completed briefing. A brand-new installation still
            # gets an immediately useful in-progress briefing until its first week ends.
            selected = (
                date.fromisoformat(latest_completed_day)
                - timedelta(days=date.fromisoformat(latest_completed_day).weekday())
                if latest_completed_day else current_week
            )
    week_end = selected + timedelta(days=6)
    through = min(today, week_end)
    elapsed_days = (through - selected).days
    prior_start = selected - timedelta(days=7)
    prior_end = prior_start + timedelta(days=elapsed_days)

    with _conn() as conn:
        first_site_clause = "AND site_id=?" if site else ""
        first_day = conn.execute(
            f"""
            SELECT MIN(day) FROM daily_filter
            WHERE include_bots=0 AND include_assets=0 AND requests>0
              {first_site_clause}
            """,
            ((site_id,) if site else ()),
        ).fetchone()[0]
        first_week = (
            date.fromisoformat(first_day) - timedelta(days=date.fromisoformat(first_day).weekday())
            if first_day else selected
        )
        available_weeks = []
        cursor = today - timedelta(days=today.weekday())
        while cursor >= first_week:
            available_weeks.append({
                "week": cursor.isoformat(),
                "to": (cursor + timedelta(days=6)).isoformat(),
                "complete": cursor + timedelta(days=6) < today,
            })
            cursor -= timedelta(days=7)

        current = _briefing_totals(conn, selected, through, site_id)
        previous = _briefing_totals(conn, prior_start, prior_end, site_id)
        site_where = "WHERE s.id=?" if site else ""
        site_current = {
            row["site"]: dict(row)
            for row in conn.execute(
                f"""
                SELECT s.name site, COALESCE(SUM(f.requests),0) requests,
                       COALESCE(SUM(f.unique_visitors),0) visitor_days
                FROM sites s LEFT JOIN daily_filter f ON f.site_id=s.id
                  AND f.day BETWEEN ? AND ? AND f.include_bots=0
                  AND f.include_assets=0
                {site_where}
                GROUP BY s.id ORDER BY s.name
                """,
                (
                    selected.isoformat(), through.isoformat(),
                    *((site_id,) if site else ()),
                ),
            )
        }
        site_previous = {
            row["site"]: row["requests"]
            for row in conn.execute(
                f"""
                SELECT s.name site, COALESCE(SUM(f.requests),0) requests
                FROM sites s LEFT JOIN daily_filter f ON f.site_id=s.id
                  AND f.day BETWEEN ? AND ? AND f.include_bots=0
                  AND f.include_assets=0
                {site_where}
                GROUP BY s.id
                """,
                (
                    prior_start.isoformat(), prior_end.isoformat(),
                    *((site_id,) if site else ()),
                ),
            )
        }
        current_pages = _briefing_page_totals(conn, selected, through, site_id)
        previous_pages = _briefing_page_totals(
            conn, prior_start, prior_end, site_id
        )
        page_changes = [
            {
                "site": site,
                "path": path,
                "requests": requests,
                "previous_requests": previous_pages.get((site, path), 0),
                "change": requests - previous_pages.get((site, path), 0),
            }
            for (site, path), requests in current_pages.items()
        ]
        page_changes.extend(
            {
                "site": site,
                "path": path,
                "requests": 0,
                "previous_requests": requests,
                "change": -requests,
            }
            for (site, path), requests in previous_pages.items()
            if (site, path) not in current_pages
        )
        page_changes.sort(
            key=lambda row: (-abs(row["change"]), -row["requests"], row["site"], row["path"])
        )

        event_site_clause = "AND e.site_id=?" if site else ""
        stored_events = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT e.kind, e.occurred_at, e.day, e.path, e.source,
                       e.agent, e.country, e.value, s.name site
                FROM events e JOIN sites s ON s.id=e.site_id
                WHERE e.day BETWEEN ? AND ?
                  {event_site_clause}
                ORDER BY e.occurred_at DESC, e.id DESC
                """,
                (
                    selected.isoformat(), through.isoformat(),
                    *((site_id,) if site else ()),
                ),
            )
        ]
        event_counts = defaultdict(int)
        for item in stored_events:
            event_counts[item["kind"]] += 1

        families = tuple(AI_AGENTS)
        placeholders = ",".join("?" for _ in families)
        ai_site_clause = "AND site_id=?" if site else ""
        ai_row = conn.execute(
            f"""
            SELECT COALESCE(SUM(requests-asset_requests),0) requests,
                   COUNT(DISTINCT ua_family) agents,
                   COUNT(DISTINCT site_id || char(0) || path) pages
            FROM daily_page_agent
            WHERE day BETWEEN ? AND ? AND is_bot=1
              AND ua_family IN ({placeholders})
              {ai_site_clause}
            """,
            (
                selected.isoformat(), through.isoformat(), *families,
                *((site_id,) if site else ()),
            ),
        ).fetchone()
        ai = dict(ai_row)

        country_site_clause = "AND site_id=?" if site else ""
        countries = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT country, SUM(human_nonasset_requests) requests
                FROM daily_country WHERE day BETWEEN ? AND ?
                  {country_site_clause}
                GROUP BY country HAVING SUM(human_nonasset_requests)>0
                ORDER BY requests DESC, country
                """,
                (
                    selected.isoformat(), through.isoformat(),
                    *((site_id,) if site else ()),
                ),
            )
        ]
        country_history_clause = "AND site_id=?" if site else ""
        new_countries = [
            row["country"] for row in countries
            if conn.execute(
                f"""
                SELECT 1 FROM daily_country
                WHERE country=? AND day<? AND human_nonasset_requests>0
                  {country_history_clause} LIMIT 1
                """,
                (
                    row["country"], selected.isoformat(),
                    *((site_id,) if site else ()),
                ),
            ).fetchone() is None
        ]
        feed_current = _feed_snapshot(conn, through, site_id)
        feed_previous = _feed_snapshot(conn, prior_end, site_id)

    summary = {
        **current,
        "previous_requests": previous["requests"],
        "previous_visitor_days": previous["visitor_days"],
        "request_change_percent": _percent_change(
            current["requests"], previous["requests"]
        ),
        "visitor_change_percent": _percent_change(
            current["visitor_days"], previous["visitor_days"]
        ),
    }
    sites = [
        {
            **row,
            "previous_requests": site_previous.get(name, 0),
            "change": row["requests"] - site_previous.get(name, 0),
            "change_percent": _percent_change(
                row["requests"], site_previous.get(name, 0)
            ),
        }
        for name, row in site_current.items()
    ]
    sites.sort(key=lambda row: (-row["requests"], row["site"]))
    discoveries = {
        "pages": event_counts["new_page"],
        "referrers": event_counts["new_referrer"],
        "resurfaced": event_counts["content_resurfaced"],
    }
    geography = {
        "countries": len(countries),
        "new_countries": new_countries,
        "top": countries[:10],
    }
    feeds = {
        **feed_current,
        "previous_reported_subscribers": feed_previous["reported_subscribers"],
        "change": (
            feed_current["reported_subscribers"]
            - feed_previous["reported_subscribers"]
        ),
    }
    return jsonify({
        "week": {
            "from": selected.isoformat(),
            "to": through.isoformat(),
            "scheduled_to": week_end.isoformat(),
            "complete": week_end < today,
            "days_compared": elapsed_days + 1,
            "previous_from": prior_start.isoformat(),
            "previous_to": prior_end.isoformat(),
        },
        "available_weeks": available_weeks,
        "summary": summary,
        "sites": sites,
        "page_changes": page_changes[:12],
        "discoveries": discoveries,
        "ai": ai,
        "geography": geography,
        "feeds": feeds,
        "moments": stored_events[:30],
        "moment_counts": dict(event_counts),
        "narrative": _briefing_narrative(
            summary, page_changes, discoveries, ai, geography, feeds
        ),
        "scope": {"site": site["name"] if site else None},
    })


def _pulse_classification(
    row: sqlite3.Row, recent_start: date, today: date
) -> tuple[str, str, Optional[int]]:
    recent = row["recent_requests"]
    weekly_baseline = row["baseline_requests"] / 4
    first_seen = date.fromisoformat(row["first_seen"])
    previous_seen = (
        date.fromisoformat(row["previous_seen"]) if row["previous_seen"] else None
    )
    quiet_days = None
    if previous_seen and row["first_recent"]:
        quiet_days = (
            date.fromisoformat(row["first_recent"]) - previous_seen
        ).days
    if recent and first_seen >= recent_start:
        return "debut", "New content receiving its first human visits.", None
    if recent and quiet_days is not None and quiet_days >= 30:
        return (
            "resurfaced",
            f"Returned after {quiet_days} quiet days.",
            quiet_days,
        )
    if recent >= 3 and recent >= max(weekly_baseline * 2, weekly_baseline + 2):
        return "rising", "Traffic is at least twice its recent weekly baseline.", None
    lifetime_span = (today - first_seen).days
    if recent and lifetime_span >= 28 and row["active_days"] >= 8:
        return "evergreen", "Still attracting visits across a long active history.", None
    if weekly_baseline >= 3 and recent <= weekly_baseline * 0.5:
        return "cooling", "Traffic fell to half or less of its recent baseline.", None
    if not recent and row["lifetime_requests"] >= 3 and row["last_seen"] < recent_start.isoformat():
        return "dormant", "Previously active, with no visits in the last seven days.", None
    return "steady", "Traffic is close to its recent pattern.", None


@api_bp.get("/pulse")
@login_required
def content_pulse():
    config = current_app.config["WEBSTATS_CONFIG"]
    today = datetime.now(ZoneInfo(config.server.timezone)).date()
    recent_start = today - timedelta(days=6)
    baseline_start = recent_start - timedelta(days=28)
    baseline_end = recent_start - timedelta(days=1)
    limit = _int_arg("limit", 100, 1, 500)

    with _conn() as conn:
        site = _requested_site(conn)
        site_clause = "AND p.site_id=?" if site else ""
        parameters: tuple[Any, ...] = (
            recent_start.isoformat(),
            today.isoformat(),
            baseline_start.isoformat(),
            baseline_end.isoformat(),
            recent_start.isoformat(),
            recent_start.isoformat(),
        )
        if site:
            parameters += (site["id"],)
        stored = conn.execute(
            f"""
            SELECT s.name site, p.path,
                   SUM(p.human_nonasset_requests) lifetime_requests,
                   SUM(CASE WHEN p.day BETWEEN ? AND ?
                       THEN p.human_nonasset_requests ELSE 0 END) recent_requests,
                   SUM(CASE WHEN p.day BETWEEN ? AND ?
                       THEN p.human_nonasset_requests ELSE 0 END) baseline_requests,
                   MIN(p.day) first_seen, MAX(p.day) last_seen,
                   MAX(CASE WHEN p.day<? THEN p.day END) previous_seen,
                   MIN(CASE WHEN p.day>=? THEN p.day END) first_recent,
                   COUNT(DISTINCT p.day) active_days
            FROM daily_page_status p JOIN sites s ON s.id=p.site_id
            WHERE p.status BETWEEN 200 AND 399
              AND p.human_nonasset_requests>0 {site_clause}
            GROUP BY p.site_id, p.path
            """,
            parameters,
        ).fetchall()

        recent_parameters: tuple[Any, ...] = (
            recent_start.isoformat(), today.isoformat()
        )
        if site:
            recent_parameters += (site["id"],)
        referrer_site_clause = "AND r.site_id=?" if site else ""
        referrers: dict[tuple[str, str], dict[str, Any]] = {}
        for row in conn.execute(
            f"""
            SELECT s.name site, r.path, r.referrer_host,
                   SUM(r.human_nonasset_requests) requests
            FROM daily_page_referrer r JOIN sites s ON s.id=r.site_id
            WHERE r.day BETWEEN ? AND ? AND r.human_nonasset_requests>0
              {referrer_site_clause}
            GROUP BY r.site_id, r.path, r.referrer_host
            ORDER BY requests DESC, r.referrer_host
            """,
            recent_parameters,
        ):
            referrers.setdefault((row["site"], row["path"]), dict(row))

        country_site_clause = "AND c.site_id=?" if site else ""
        countries: dict[tuple[str, str], dict[str, Any]] = {}
        for row in conn.execute(
            f"""
            SELECT s.name site, c.path, c.country,
                   SUM(c.human_nonasset_requests) requests
            FROM daily_page_country c JOIN sites s ON s.id=c.site_id
            WHERE c.day BETWEEN ? AND ? AND c.human_nonasset_requests>0
              {country_site_clause}
            GROUP BY c.site_id, c.path, c.country
            ORDER BY requests DESC, c.country
            """,
            recent_parameters,
        ):
            countries.setdefault((row["site"], row["path"]), dict(row))

        families = tuple(AI_AGENTS)
        placeholders = ",".join("?" for _ in families)
        ai_site_clause = "AND a.site_id=?" if site else ""
        ai_parameters: tuple[Any, ...] = (
            recent_start.isoformat(), today.isoformat(), *families
        )
        if site:
            ai_parameters += (site["id"],)
        ai_requests = {
            (row["site"], row["path"]): row["requests"]
            for row in conn.execute(
                f"""
                SELECT s.name site, a.path,
                       SUM(a.requests-a.asset_requests) requests
                FROM daily_page_agent a JOIN sites s ON s.id=a.site_id
                WHERE a.day BETWEEN ? AND ? AND a.is_bot=1
                  AND a.ua_family IN ({placeholders}) {ai_site_clause}
                GROUP BY a.site_id, a.path
                HAVING SUM(a.requests-a.asset_requests)>0
                """,
                ai_parameters,
            )
        }

    pages = []
    counts: dict[str, int] = defaultdict(int)
    priority = {
        "resurfaced": 0, "rising": 1, "debut": 2, "evergreen": 3,
        "cooling": 4, "dormant": 5, "steady": 6,
    }
    for row in stored:
        category, explanation, quiet_days = _pulse_classification(
            row, recent_start, today
        )
        counts[category] += 1
        baseline_weekly = round(row["baseline_requests"] / 4, 1)
        change = round(row["recent_requests"] - baseline_weekly, 1)
        referrer = referrers.get((row["site"], row["path"]))
        country = countries.get((row["site"], row["path"]))
        pages.append({
            "site": row["site"],
            "path": row["path"],
            "category": category,
            "explanation": explanation,
            "recent_requests": row["recent_requests"],
            "weekly_baseline": baseline_weekly,
            "change": change,
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "active_days": row["active_days"],
            "lifetime_requests": row["lifetime_requests"],
            "quiet_days": quiet_days,
            "top_referrer": referrer["referrer_host"] if referrer else None,
            "top_country": country["country"] if country else None,
            "ai_requests": ai_requests.get((row["site"], row["path"]), 0),
        })
    pages.sort(key=lambda row: (
        priority[row["category"]], -abs(row["change"]),
        -row["recent_requests"], row["site"], row["path"],
    ))
    signals = [row for row in pages if row["category"] != "steady"]
    return jsonify({
        "scope": {"site": site["name"] if site else None},
        "window": {
            "from": recent_start.isoformat(),
            "to": today.isoformat(),
            "baseline_from": baseline_start.isoformat(),
            "baseline_to": baseline_end.isoformat(),
        },
        "counts": {name: counts.get(name, 0) for name in priority},
        "signals": signals[:limit],
        "steady": [row for row in pages if row["category"] == "steady"][:10],
        "total_pages": len(pages),
        "limited": len(signals) > limit,
    })


@api_bp.get("/errors")
@login_required
def error_intelligence():
    config = current_app.config["WEBSTATS_CONFIG"]
    today = datetime.now(ZoneInfo(config.server.timezone)).date()
    days = _int_arg("days", 30, 7, 365)
    limit = _int_arg("limit", 100, 1, 500)
    start = today - timedelta(days=days - 1)

    with _conn() as conn:
        site = _requested_site(conn)
        site_clause = "AND d.site_id=?" if site else ""
        parameters: tuple[Any, ...] = (
            start.isoformat(), today.isoformat(),
            start.isoformat(), today.isoformat(),
            start.isoformat(), today.isoformat(),
        )
        if site:
            parameters += (site["id"],)
        misses = conn.execute(
            f"""
            SELECT s.name site, d.site_id, d.path,
                   SUM(CASE WHEN d.day BETWEEN ? AND ?
                       THEN d.human_nonasset_requests ELSE 0 END) requests,
                   COUNT(DISTINCT CASE WHEN d.day BETWEEN ? AND ?
                       AND d.human_nonasset_requests>0 THEN d.day END) active_days,
                   MIN(CASE WHEN d.human_nonasset_requests>0 THEN d.day END) first_seen,
                   MAX(CASE WHEN d.day BETWEEN ? AND ?
                       AND d.human_nonasset_requests>0 THEN d.day END) last_seen
            FROM daily_404 d JOIN sites s ON s.id=d.site_id
            WHERE d.human_nonasset_requests>0 {site_clause}
            GROUP BY d.site_id, d.path
            HAVING SUM(CASE WHEN d.day BETWEEN ? AND ?
                       THEN d.human_nonasset_requests ELSE 0 END)>0
            """,
            parameters + (start.isoformat(), today.isoformat()),
        ).fetchall()

        success_parameters: tuple[Any, ...] = ()
        success_clause = "AND p.site_id=?" if site else ""
        if site:
            success_parameters = (site["id"],)
        successes: dict[tuple[int, str], dict[str, Any]] = {}
        successful_paths: dict[
            int, dict[str, list[tuple[str, int]]]
        ] = defaultdict(lambda: defaultdict(list))
        successful_path_counts: dict[int, int] = defaultdict(int)
        for row in conn.execute(
            f"""
            SELECT p.site_id, p.path, MAX(p.day) last_success,
                   SUM(p.human_nonasset_requests) requests
            FROM daily_page_status p
            WHERE p.status BETWEEN 200 AND 399
              AND p.human_nonasset_requests>0 {success_clause}
            GROUP BY p.site_id, p.path
            ORDER BY requests DESC
            """,
            success_parameters,
        ):
            successes[(row["site_id"], row["path"])] = dict(row)
            if successful_path_counts[row["site_id"]] < 2000:
                for gram in _path_grams(row["path"]):
                    successful_paths[row["site_id"]][gram].append(
                        (row["path"], row["requests"])
                    )
                successful_path_counts[row["site_id"]] += 1

        raw_parameters: tuple[Any, ...] = (
            start.isoformat(), today.isoformat()
        )
        raw_site_clause = "AND r.site_id=?" if site else ""
        if site:
            raw_parameters += (site["id"],)
        referrers: dict[tuple[int, str], dict[str, Any]] = {}
        for row in conn.execute(
            f"""
            SELECT r.site_id, r.path, r.referrer_host,
                   COUNT(*) requests
            FROM requests r
            WHERE r.day BETWEEN ? AND ? AND r.status=404
              AND r.is_bot=0 AND r.is_asset=0
              AND r.referrer_host IS NOT NULL {raw_site_clause}
            GROUP BY r.site_id, r.path, r.referrer_host
            ORDER BY requests DESC, r.referrer_host
            """,
            raw_parameters,
        ):
            referrers.setdefault((row["site_id"], row["path"]), dict(row))
        countries: dict[tuple[int, str], dict[str, Any]] = {}
        for row in conn.execute(
            f"""
            SELECT r.site_id, r.path, r.country, COUNT(*) requests
            FROM requests r
            WHERE r.day BETWEEN ? AND ? AND r.status=404
              AND r.is_bot=0 AND r.is_asset=0
              AND r.country IS NOT NULL {raw_site_clause}
            GROUP BY r.site_id, r.path, r.country
            ORDER BY requests DESC, r.country
            """,
            raw_parameters,
        ):
            countries.setdefault((row["site_id"], row["path"]), dict(row))

    signals = []
    probes = []
    counts: dict[str, int] = defaultdict(int)
    total_requests = 0
    probe_requests = 0
    priority = {"regression": 0, "typo": 1, "persistent": 2, "new": 3, "watch": 4}
    for row in misses:
        item = {
            key: row[key]
            for key in (
                "site", "path", "requests", "active_days", "first_seen", "last_seen"
            )
        }
        total_requests += row["requests"]
        if _is_probe_path(row["path"]):
            probe_requests += row["requests"]
            probes.append(item)
            continue

        success = successes.get((row["site_id"], row["path"]))
        regression = bool(
            success and success["last_success"] < row["last_seen"]
        )
        path_length = len(row["path"].lower().rstrip("/") or "/")
        candidate_matches: dict[str, list[int]] = {}
        for gram in _path_grams(row["path"]):
            for candidate, requests in successful_paths[row["site_id"]].get(
                gram, ()
            ):
                if abs(len(candidate.lower().rstrip("/") or "/") - path_length) > 2:
                    continue
                match = candidate_matches.setdefault(candidate, [requests, 0])
                match[1] += 1
        plausible_paths = (
            (candidate, values[0])
            for candidate, values in sorted(
                candidate_matches.items(),
                key=lambda item: (-item[1][1], -item[1][0], item[0]),
            )[:100]
        )
        suggestion = None if success else _typo_candidate(
            row["path"], plausible_paths
        )
        if regression:
            category = "regression"
            explanation = (
                f"This path previously worked; its last successful day was "
                f"{success['last_success']}."
            )
        elif suggestion:
            category = "typo"
            explanation = f"This resembles the working path {suggestion['path']}."
        elif row["active_days"] >= 3 or row["requests"] >= 5:
            category = "persistent"
            explanation = (
                f"Repeated on {row['active_days']} day"
                f"{'s' if row['active_days'] != 1 else ''}; likely a stale link "
                "or recurring request."
            )
        elif row["first_seen"] >= start.isoformat():
            category = "new"
            explanation = f"First appeared on {row['first_seen']}."
        else:
            category = "watch"
            explanation = "An occasional miss without enough evidence to diagnose yet."
        counts[category] += 1
        referrer = referrers.get((row["site_id"], row["path"]))
        country = countries.get((row["site_id"], row["path"]))
        signals.append({
            **item,
            "category": category,
            "explanation": explanation,
            "last_success": success["last_success"] if success else None,
            "suggestion": suggestion,
            "top_referrer": referrer["referrer_host"] if referrer else None,
            "top_country": country["country"] if country else None,
        })

    signals.sort(key=lambda row: (
        priority[row["category"]], -row["requests"], -row["active_days"],
        row["site"], row["path"],
    ))
    probes.sort(key=lambda row: (-row["requests"], row["site"], row["path"]))
    actionable_requests = total_requests - probe_requests
    return jsonify({
        "scope": {"site": site["name"] if site else None},
        "window": {"from": start.isoformat(), "to": today.isoformat(), "days": days},
        "summary": {
            "miss_requests": total_requests,
            "actionable_requests": actionable_requests,
            "actionable_paths": len(signals),
            "probe_requests": probe_requests,
            "noise_percent": round(100 * probe_requests / total_requests, 1)
            if total_requests else 0.0,
            "regressions": counts["regression"],
            "typo_candidates": counts["typo"],
            "persistent": counts["persistent"],
        },
        "counts": {name: counts.get(name, 0) for name in priority},
        "signals": signals[:limit],
        "signals_limited": len(signals) > limit,
        "probes": probes[:25],
        "probes_limited": len(probes) > 25,
    })


@api_bp.get("/journeys")
@login_required
def journeys():
    start, end = _date_range()
    with _conn() as conn:
        site = _requested_site(conn)
        is_private = bool(
            site and site["name"] in INDIVIDUAL_ACTIVITY_PRIVATE_SITES
        )
        if is_private:
            return jsonify({
                "scope": {"site": site["name"]},
                "from": start,
                "to": end,
                "privacy": {
                    "protected": True,
                    "excluded_sites": list(INDIVIDUAL_ACTIVITY_PRIVATE_SITES),
                },
                "totals": {
                    "sessions": 0, "pageviews": 0, "path_steps": 0,
                    "single_page_sessions": 0, "multi_page_sessions": 0,
                    "max_depth": 0, "duration_seconds": 0,
                    "average_depth": 0.0, "multi_page_rate": 0.0,
                    "average_duration_seconds": 0,
                },
                "series": [], "entrances": [], "exits": [], "transitions": [],
            })

        private_placeholders = ",".join(
            "?" for _ in INDIVIDUAL_ACTIVITY_PRIVATE_SITES
        )
        site_clause = "AND j.site_id=?" if site else ""
        parameters: tuple[Any, ...] = (
            start, end, *INDIVIDUAL_ACTIVITY_PRIVATE_SITES,
            *((site["id"],) if site else ()),
        )
        total_row = conn.execute(
            f"""
            SELECT COALESCE(SUM(j.sessions),0) sessions,
                   COALESCE(SUM(j.pageviews),0) pageviews,
                   COALESCE(SUM(j.path_steps),0) path_steps,
                   COALESCE(SUM(j.single_page_sessions),0) single_page_sessions,
                   COALESCE(SUM(j.multi_page_sessions),0) multi_page_sessions,
                   COALESCE(MAX(j.max_depth),0) max_depth,
                   COALESCE(SUM(j.duration_seconds),0) duration_seconds
            FROM daily_journey j JOIN sites s ON s.id=j.site_id
            WHERE j.day BETWEEN ? AND ?
              AND s.name NOT IN ({private_placeholders}) {site_clause}
            """,
            parameters,
        ).fetchone()
        totals = dict(total_row)
        sessions = totals["sessions"]
        totals["average_depth"] = round(
            totals["path_steps"] / sessions, 2
        ) if sessions else 0.0
        totals["multi_page_rate"] = round(
            100 * totals["multi_page_sessions"] / sessions, 1
        ) if sessions else 0.0
        totals["average_duration_seconds"] = round(
            totals["duration_seconds"] / sessions
        ) if sessions else 0

        series_rows = conn.execute(
            f"""
            SELECT j.day bucket, SUM(j.sessions) sessions,
                   SUM(j.multi_page_sessions) multi_page_sessions,
                   SUM(j.path_steps) path_steps
            FROM daily_journey j JOIN sites s ON s.id=j.site_id
            WHERE j.day BETWEEN ? AND ?
              AND s.name NOT IN ({private_placeholders}) {site_clause}
            GROUP BY j.day ORDER BY j.day
            """,
            parameters,
        )
        series_by_day = {row["bucket"]: dict(row) for row in series_rows}
        series = [
            series_by_day.get(
                day,
                {
                    "bucket": day, "sessions": 0,
                    "multi_page_sessions": 0, "path_steps": 0,
                },
            )
            for day in _day_buckets(start, end)
        ]

        endpoint_site_clause = "AND e.site_id=?" if site else ""
        endpoint_parameters = (
            start, end, *INDIVIDUAL_ACTIVITY_PRIVATE_SITES,
            *((site["id"],) if site else ()),
        )
        entrances = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT s.name site, e.path, SUM(e.entrances) visits,
                       SUM(e.single_page_sessions) single_page_visits
                FROM daily_journey_endpoint e JOIN sites s ON s.id=e.site_id
                WHERE e.day BETWEEN ? AND ?
                  AND s.name NOT IN ({private_placeholders}) {endpoint_site_clause}
                GROUP BY e.site_id, e.path HAVING SUM(e.entrances)>0
                ORDER BY visits DESC, s.name, e.path LIMIT 25
                """,
                endpoint_parameters,
            )
        ]
        exits = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT s.name site, e.path, SUM(e.exits) visits
                FROM daily_journey_endpoint e JOIN sites s ON s.id=e.site_id
                WHERE e.day BETWEEN ? AND ?
                  AND s.name NOT IN ({private_placeholders}) {endpoint_site_clause}
                GROUP BY e.site_id, e.path HAVING SUM(e.exits)>0
                ORDER BY visits DESC, s.name, e.path LIMIT 25
                """,
                endpoint_parameters,
            )
        ]
        transition_site_clause = "AND t.site_id=?" if site else ""
        transitions = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT s.name site, t.from_path, t.to_path,
                       SUM(t.transitions) transitions
                FROM daily_journey_transition t JOIN sites s ON s.id=t.site_id
                WHERE t.day BETWEEN ? AND ?
                  AND s.name NOT IN ({private_placeholders}) {transition_site_clause}
                GROUP BY t.site_id, t.from_path, t.to_path
                HAVING SUM(t.transitions)>0
                ORDER BY transitions DESC, s.name, t.from_path, t.to_path
                LIMIT 50
                """,
                endpoint_parameters,
            )
        ]
    return jsonify({
        "scope": {"site": site["name"] if site else None},
        "from": start,
        "to": end,
        "privacy": {
            "protected": False,
            "excluded_sites": list(INDIVIDUAL_ACTIVITY_PRIVATE_SITES),
        },
        "totals": totals,
        "series": series,
        "entrances": entrances,
        "exits": exits,
        "transitions": transitions,
    })


@api_bp.get("/almanac")
@login_required
def almanac():
    config = current_app.config["WEBSTATS_CONFIG"]
    today = datetime.now(ZoneInfo(config.server.timezone)).date()
    year = _year_arg()
    bots, assets = _flags()
    requested_site = request.args.get("site", "").strip()

    with _conn() as conn:
        site = _site(conn, requested_site) if requested_site else None
        site_clause = "AND f.site_id=?" if site else ""
        parameters: tuple[Any, ...] = (bots, assets)
        if site:
            parameters += (site["id"],)
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT f.day, SUM(f.requests) requests,
                       SUM(f.unique_visitors) visitor_days
                FROM daily_filter f
                WHERE f.include_bots=? AND f.include_assets=? {site_clause}
                GROUP BY f.day HAVING SUM(f.requests)>0 ORDER BY f.day
                """,
                parameters,
            )
        ]
        sites = [row["name"] for row in conn.execute("SELECT name FROM sites ORDER BY name")]

    values = {row["day"]: row for row in rows}
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    days = []
    cursor = year_start
    while cursor <= year_end:
        stored = values.get(cursor.isoformat(), {})
        days.append({
            "day": cursor.isoformat(),
            "requests": stored.get("requests", 0),
            "visitor_days": stored.get("visitor_days", 0),
            "future": cursor > today,
        })
        cursor += timedelta(days=1)

    dated = [(date.fromisoformat(row["day"]), row) for row in rows]
    active = [day for day, _ in dated]
    streaks = _streaks(active, today)
    busiest = None
    if rows:
        busiest = min(rows, key=lambda row: (-row["requests"], row["day"]))

    weeks: dict[str, dict[str, int]] = defaultdict(
        lambda: {"requests": 0, "visitor_days": 0}
    )
    months: dict[str, dict[str, int]] = defaultdict(
        lambda: {"requests": 0, "visitor_days": 0}
    )
    record_breakers = []
    record = -1
    cumulative_visitors = 0
    milestone_targets = [
        100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000,
        100000, 250000, 500000, 1000000,
    ]
    reached = []
    target_index = 0
    for day, row in dated:
        week_start = day - timedelta(days=day.weekday())
        week = weeks[week_start.isoformat()]
        month = months[day.strftime("%Y-%m")]
        for bucket in (week, month):
            bucket["requests"] += row["requests"]
            bucket["visitor_days"] += row["visitor_days"]
        if row["requests"] > record:
            record = row["requests"]
            record_breakers.append({
                "day": row["day"],
                "requests": row["requests"],
                "visitor_days": row["visitor_days"],
            })
        cumulative_visitors += row["visitor_days"]
        while (
            target_index < len(milestone_targets)
            and cumulative_visitors >= milestone_targets[target_index]
        ):
            reached.append({
                "value": milestone_targets[target_index],
                "day": row["day"],
            })
            target_index += 1

    total_visitors = sum(row["visitor_days"] for row in rows)
    next_target = next(
        (target for target in milestone_targets if target > total_visitors), None
    )
    if next_target is None:
        next_target = ((total_visitors // 1_000_000) + 1) * 1_000_000
    month_day = today.strftime("-%m-%d")
    on_this_day = [row for row in rows if row["day"].endswith(month_day)]
    available_years = sorted(
        {date.fromisoformat(row["day"]).year for row in rows} | {today.year},
        reverse=True,
    )
    totals = {
        "requests": sum(row["requests"] for row in rows),
        "visitor_days": total_visitors,
        "active_days": len(rows),
        "first_seen": rows[0]["day"] if rows else None,
        "last_seen": rows[-1]["day"] if rows else None,
    }
    return jsonify({
        "scope": {"site": requested_site or None, "label": requested_site or "All sites"},
        "sites": sites,
        "year": year,
        "available_years": available_years,
        "days": days,
        "totals": totals,
        "records": {
            "busiest_day": busiest,
            "best_week": _best_period(weeks),
            "best_month": _best_period(months),
            **streaks,
        },
        "milestones": {
            "reached": reached,
            "next": next_target,
            "progress": round(100 * total_visitors / next_target, 1),
        },
        "record_breakers": list(reversed(record_breakers)),
        "on_this_day": on_this_day,
    })


@api_bp.get("/overview")
@login_required
def overview():
    start, end = _date_range()
    bots, assets = _flags()
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    span = (end_date - start_date).days + 1
    prior_end = start_date - timedelta(days=1)
    prior_start = prior_end - timedelta(days=span - 1)
    with _conn() as conn:
        current = {
            row["name"]: dict(row)
            for row in conn.execute(
                """
                SELECT s.name, COALESCE(SUM(f.requests),0) requests,
                       COALESCE(SUM(f.unique_visitors),0) unique_visitors,
                       COALESCE(SUM(f.bytes),0) bytes,
                       COALESCE(SUM(f.status_4xx),0) status_4xx,
                       COALESCE(SUM(f.status_5xx),0) status_5xx
                FROM sites s LEFT JOIN daily_filter f ON f.site_id=s.id
                  AND f.day BETWEEN ? AND ? AND f.include_bots=? AND f.include_assets=?
                GROUP BY s.id ORDER BY s.name
                """,
                (start, end, bots, assets),
            )
        }
        prior = {
            row["name"]: row["requests"]
            for row in conn.execute(
                """
                SELECT s.name, COALESCE(SUM(f.requests),0) requests
                FROM sites s LEFT JOIN daily_filter f ON f.site_id=s.id
                  AND f.day BETWEEN ? AND ? AND f.include_bots=? AND f.include_assets=?
                GROUP BY s.id
                """,
                (prior_start.isoformat(), prior_end.isoformat(), bots, assets),
            )
        }
        stored_series = [
            dict(row)
            for row in conn.execute(
                """
                SELECT f.day, s.name site, f.requests, f.unique_visitors
                FROM daily_filter f JOIN sites s ON s.id=f.site_id
                WHERE f.day BETWEEN ? AND ? AND f.include_bots=? AND f.include_assets=?
                ORDER BY f.day, s.name
                """,
                (start, end, bots, assets),
            )
        ]
        bot_totals = conn.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN is_bot=1 THEN requests ELSE 0 END),0),
                   COALESCE(SUM(requests),0)
            FROM daily_traffic WHERE day BETWEEN ? AND ?
            """,
            (start, end),
        ).fetchone()
    series_values = {
        (row["day"], row["site"]): row for row in stored_series
    }
    series = [
        series_values.get(
            (day, name),
            {"day": day, "site": name, "requests": 0, "unique_visitors": 0},
        )
        for day in _day_buckets(start, end)
        for name in current
    ]
    cards = []
    for name, row in current.items():
        previous = prior.get(name, 0)
        change = None if previous == 0 else round((row["requests"] - previous) * 100 / previous, 1)
        cards.append({**row, "change_percent": change})
    total_requests = sum(item["requests"] for item in cards)
    client_errors = sum(item["status_4xx"] for item in cards)
    server_errors = sum(item["status_5xx"] for item in cards)
    totals = {
        "requests": total_requests,
        "unique_visitors": sum(item["unique_visitors"] for item in cards),
        "bytes": sum(item["bytes"] for item in cards),
        "client_error_rate": round(100 * client_errors / max(1, total_requests), 2),
        "server_error_rate": round(100 * server_errors / max(1, total_requests), 2),
        "error_rate": round(
            100 * (client_errors + server_errors) / max(1, total_requests), 2
        ),
        "bot_share": round(100 * bot_totals[0] / max(1, bot_totals[1]), 2),
    }
    return jsonify({"from": start, "to": end, "sites": cards, "timeseries": series, "totals": totals})


@api_bp.get("/site/<path:name>/timeseries")
@login_required
def timeseries(name: str):
    start, end = _date_range()
    bots, assets = _flags()
    interval = request.args.get("interval", "day")
    if interval == "hour":
        config = current_app.config["WEBSTATS_CONFIG"]
        today = datetime.now(ZoneInfo(config.server.timezone)).date()
        raw_cutoff = today - timedelta(days=config.storage.raw_retention_days)
        if date.fromisoformat(start) < raw_cutoff:
            interval = "day"
    with _conn() as conn:
        site = _site(conn, name)
        if interval == "day":
            stored = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT day bucket, requests, unique_visitors, bytes
                    FROM daily_filter WHERE site_id=? AND day BETWEEN ? AND ?
                      AND include_bots=? AND include_assets=? ORDER BY day
                    """,
                    (site["id"], start, end, bots, assets),
                )
            ]
            values = {row["bucket"]: row for row in stored}
            rows = [
                values.get(
                    bucket,
                    {
                        "bucket": bucket,
                        "requests": 0,
                        "unique_visitors": 0,
                        "bytes": 0,
                    },
                )
                for bucket in _day_buckets(start, end)
            ]
        elif interval == "hour":
            if (date.fromisoformat(end) - date.fromisoformat(start)).days > 31:
                raise ApiError("Hourly ranges cannot exceed 32 days")
            raw = conn.execute(
                """
                SELECT ts, ip_hash, bytes FROM requests
                WHERE site_id=? AND day BETWEEN ? AND ?
                  AND (?=1 OR is_bot=0) AND (?=1 OR is_asset=0) ORDER BY ts
                """,
                (site["id"], start, end, bots, assets),
            ).fetchall()
            zone = ZoneInfo(current_app.config["WEBSTATS_CONFIG"].server.timezone)
            groups: dict[str, dict[str, Any]] = {}
            for row in raw:
                bucket = datetime.fromtimestamp(row["ts"], zone).strftime("%Y-%m-%dT%H:00:00%z")
                item = groups.setdefault(bucket, {"bucket": bucket, "requests": 0, "bytes": 0, "visitors": set()})
                item["requests"] += 1
                item["bytes"] += row["bytes"]
                item["visitors"].add(row["ip_hash"])
            rows = [
                {
                    "bucket": bucket,
                    "requests": groups.get(bucket, {}).get("requests", 0),
                    "bytes": groups.get(bucket, {}).get("bytes", 0),
                    "unique_visitors": len(groups.get(bucket, {}).get("visitors", set())),
                }
                for bucket in _hour_buckets(start, end, zone)
            ]
        else:
            raise ApiError("interval must be day or hour")
    return jsonify({"site": name, "interval": interval, "series": rows})


@api_bp.get("/site/<path:name>/pages")
@login_required
def pages(name: str):
    start, end = _date_range()
    bots, assets = _flags()
    limit = _int_arg("limit", 25, 1, 100)
    offset = _int_arg("offset", 0, 0, 1_000_000)
    request_col = "requests" if bots else "human_requests"
    unique_col = "unique_visitors" if bots else "human_unique_visitors"
    asset_clause = "" if assets else "AND asset_requests=0"
    with _conn() as conn:
        site = _site(conn, name)
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT path, SUM({request_col}) requests, SUM({unique_col}) unique_visitors
                FROM daily_path WHERE site_id=? AND day BETWEEN ? AND ? {asset_clause}
                GROUP BY path HAVING SUM({request_col})>0
                ORDER BY requests DESC, path LIMIT ? OFFSET ?
                """,
                (site["id"], start, end, limit, offset),
            )
        ]
    return jsonify({"site": name, "pages": rows, "limit": limit, "offset": offset})


@api_bp.get("/site/<path:name>/page")
@login_required
def page_detail(name: str):
    start, end = _date_range()
    path = _path_arg()
    bots, assets = _flags()
    request_col = "requests" if bots else "human_requests"
    unique_col = "unique_visitors" if bots else "human_unique_visitors"
    metric = _metric_column(bots, assets)
    asset_clause = "" if assets else "AND asset_requests=0"
    agent_value = "requests" if assets else "requests-asset_requests"
    families = tuple(AI_AGENTS)
    placeholders = ",".join("?" for _ in families)

    with _conn() as conn:
        site = _site(conn, name)
        known = conn.execute(
            "SELECT 1 FROM daily_path WHERE site_id=? AND path=? LIMIT 1",
            (site["id"], path),
        ).fetchone()
        if known is None:
            raise ApiError("Unknown page path", 404)

        stored_series = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT day bucket, SUM({request_col}) requests,
                       SUM({unique_col}) unique_visitors
                FROM daily_path
                WHERE site_id=? AND path=? AND day BETWEEN ? AND ? {asset_clause}
                GROUP BY day ORDER BY day
                """,
                (site["id"], path, start, end),
            )
        ]
        values = {row["bucket"]: row for row in stored_series}
        series = [
            values.get(
                day,
                {"bucket": day, "requests": 0, "unique_visitors": 0},
            )
            for day in _day_buckets(start, end)
        ]
        lifetime = conn.execute(
            f"""
            SELECT MIN(day) first_seen, MAX(day) last_seen
            FROM daily_path
            WHERE site_id=? AND path=? AND {request_col}>0 {asset_clause}
            """,
            (site["id"], path),
        ).fetchone()

        referrer_rows = conn.execute(
            f"""
            SELECT referrer_host,
                   SUM(CASE WHEN day BETWEEN ? AND ? THEN {metric} ELSE 0 END) requests,
                   MIN(CASE WHEN {metric}>0 THEN day END) first_seen,
                   MAX(CASE WHEN {metric}>0 THEN day END) last_seen
            FROM daily_page_referrer
            WHERE site_id=? AND path=?
            GROUP BY referrer_host
            HAVING SUM(CASE WHEN day BETWEEN ? AND ? THEN {metric} ELSE 0 END)>0
            """,
            (start, end, site["id"], path, start, end),
        )
        grouped_referrers: dict[str, dict[str, Any]] = {}
        for row in referrer_rows:
            group = _referrer_group(row["referrer_host"])
            stored = grouped_referrers.setdefault(group, {
                "group": group, "requests": 0,
                "first_seen": row["first_seen"], "last_seen": row["last_seen"],
            })
            stored["requests"] += row["requests"]
            stored["first_seen"] = min(stored["first_seen"], row["first_seen"])
            stored["last_seen"] = max(stored["last_seen"], row["last_seen"])
        referrers = [
            details
            for _, details in sorted(
                grouped_referrers.items(),
                key=lambda item: (-item[1]["requests"], item[0]),
            )
        ][:25]

        countries = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT country, SUM({metric}) requests FROM daily_page_country
                WHERE site_id=? AND path=? AND day BETWEEN ? AND ?
                GROUP BY country HAVING SUM({metric})>0
                ORDER BY requests DESC, country LIMIT 25
                """,
                (site["id"], path, start, end),
            )
        ]
        statuses = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT status, SUM({metric}) requests FROM daily_page_status
                WHERE site_id=? AND path=? AND day BETWEEN ? AND ?
                GROUP BY status HAVING SUM({metric})>0 ORDER BY status
                """,
                (site["id"], path, start, end),
            )
        ]
        agent_rows = conn.execute(
            f"""
            SELECT ua_family agent, SUM({agent_value}) requests,
                   MIN(day) first_seen, MAX(day) last_seen
            FROM daily_page_agent
            WHERE site_id=? AND path=? AND day BETWEEN ? AND ? AND is_bot=1
              AND ua_family IN ({placeholders})
            GROUP BY ua_family HAVING SUM({agent_value})>0
            ORDER BY requests DESC, agent
            """,
            (site["id"], path, start, end, *families),
        )
        ai_agents = []
        for row in agent_rows:
            provider, purpose = AI_AGENTS[row["agent"]]
            ai_agents.append({**dict(row), "provider": provider, "purpose": purpose})

        journey_private = name in INDIVIDUAL_ACTIVITY_PRIVATE_SITES
        if journey_private:
            journey = {
                "protected": True, "entrances": 0, "exits": 0,
                "previous": [], "next": [],
            }
        else:
            journey_endpoint = conn.execute(
                """
                SELECT COALESCE(SUM(entrances),0) entrances,
                       COALESCE(SUM(exits),0) exits
                FROM daily_journey_endpoint
                WHERE site_id=? AND path=? AND day BETWEEN ? AND ?
                """,
                (site["id"], path, start, end),
            ).fetchone()
            previous = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT from_path path, SUM(transitions) transitions
                    FROM daily_journey_transition
                    WHERE site_id=? AND to_path=? AND day BETWEEN ? AND ?
                    GROUP BY from_path ORDER BY transitions DESC, from_path LIMIT 15
                    """,
                    (site["id"], path, start, end),
                )
            ]
            following = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT to_path path, SUM(transitions) transitions
                    FROM daily_journey_transition
                    WHERE site_id=? AND from_path=? AND day BETWEEN ? AND ?
                    GROUP BY to_path ORDER BY transitions DESC, to_path LIMIT 15
                    """,
                    (site["id"], path, start, end),
                )
            ]
            journey = {
                "protected": False,
                "entrances": journey_endpoint["entrances"],
                "exits": journey_endpoint["exits"],
                "previous": previous,
                "next": following,
            }

    totals = {
        "requests": sum(row["requests"] for row in series),
        "unique_visitors": sum(row["unique_visitors"] for row in series),
        "first_seen": lifetime["first_seen"],
        "last_seen": lifetime["last_seen"],
    }
    return jsonify({
        "site": name,
        "path": path,
        "from": start,
        "to": end,
        "series": series,
        "totals": totals,
        "referrers": referrers,
        "countries": countries,
        "statuses": statuses,
        "ai_agents": ai_agents,
        "journey": journey,
    })


def _ranked_endpoint(name: str, table: str, dimension: str, output: str):
    start, end = _date_range()
    bots, assets = _flags()
    metric = _metric_column(bots, assets)
    limit = _int_arg("limit", 25, 1, 100)
    with _conn() as conn:
        site = _site(conn, name)
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT {dimension}, SUM({metric}) requests FROM {table}
                WHERE site_id=? AND day BETWEEN ? AND ?
                GROUP BY {dimension} HAVING SUM({metric})>0
                ORDER BY requests DESC, {dimension} LIMIT ?
                """,
                (site["id"], start, end, limit),
            )
        ]
    return jsonify({"site": name, output: rows})


@api_bp.get("/site/<path:name>/referrers")
@login_required
def referrers(name: str):
    start, end = _date_range()
    bots, assets = _flags()
    metric = _metric_column(bots, assets)
    limit = _int_arg("limit", 25, 1, 100)
    with _conn() as conn:
        site = _site(conn, name)
        rows = conn.execute(
            f"""
            SELECT referrer_host, SUM({metric}) requests FROM daily_referrer
            WHERE site_id=? AND day BETWEEN ? AND ?
            GROUP BY referrer_host HAVING SUM({metric})>0
            """,
            (site["id"], start, end),
        )
        body = {"site": name}
    grouped: dict[str, int] = defaultdict(int)
    for row in rows:
        grouped[_referrer_group(row["referrer_host"])] += row["requests"]
    body["referrers"] = [
        {"group": group, "requests": count}
        for group, count in sorted(grouped.items(), key=lambda item: (-item[1], item[0]))
    ][:limit]
    return jsonify(body)


def _referrer_group(host: str) -> str:
    value = host.lower().rstrip(".")
    if re.search(r"(?:^|\.)google\.(?:[a-z]{2,3}|com?\.[a-z]{2})\Z", value):
        return "Google"
    groups = (
        (("bing.com",), "Bing"),
        (("duckduckgo.com", "duck.com"), "DuckDuckGo"),
        (("search.yahoo.com", "search.yahoo.co.jp", "search.yahoo.co.uk"), "Yahoo"),
        (("facebook.com", "instagram.com", "threads.net"), "Meta"),
        (("x.com", "twitter.com", "t.co"), "X / Twitter"),
        (("linkedin.com",), "LinkedIn"),
    )
    for domains, label in groups:
        if any(value == domain or value.endswith(f".{domain}") for domain in domains):
            return label
    return host


def _source_arg() -> str:
    value = request.args.get("source", "").strip().lower().rstrip(".")
    if not value:
        return ""
    if (
        len(value) > 253
        or re.search(r"\s", value)
        or any(character in value for character in "/?#@")
    ):
        raise ApiError("source must be a referring host")
    return value


def _link_source_category(row: dict[str, Any], start: date) -> tuple[str, str]:
    current = row["requests"]
    previous = row["prior_requests"]
    first_seen = date.fromisoformat(row["first_seen"])
    previous_seen = (
        date.fromisoformat(row["previous_seen"]) if row["previous_seen"] else None
    )
    first_current = (
        date.fromisoformat(row["first_current"]) if row["first_current"] else None
    )
    if current and first_seen >= start:
        return "debut", "First appeared in this date range"
    if (
        current and previous_seen and first_current
        and (first_current - previous_seen).days >= 30
    ):
        return "resurfaced", f"Returned after {(first_current - previous_seen).days} quiet days"
    if current >= 3 and current >= max(previous * 2, previous + 2):
        return "rising", "At least twice the preceding period"
    if current and row["active_days"] >= 5:
        return "loyal", f"Active on {row['active_days']} days all time"
    if current and row["active_days"] == 1:
        return "one-off", "Seen on a single day so far"
    if not current and previous:
        return "dormant", "Present in the preceding period, quiet now"
    if previous >= 3 and current * 2 <= previous:
        return "cooling", "Less than half the preceding period"
    return "steady", "Moving within its recent pattern"


@api_bp.get("/link-atlas")
@login_required
def link_atlas():
    start_text, end_text = _date_range()
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(end_text)
    span = (end - start).days + 1
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=span - 1)
    selected_source = _source_arg()
    limit = _int_arg("limit", 50, 1, 100)
    private_placeholders = ",".join(
        "?" for _ in INDIVIDUAL_ACTIVITY_PRIVATE_SITES
    )

    with _conn() as conn:
        site = _requested_site(conn)
        if site and site["name"] in INDIVIDUAL_ACTIVITY_PRIVATE_SITES:
            return jsonify({
                "scope": {"site": site["name"], "source": selected_source or None},
                "from": start_text, "to": end_text,
                "previous": {
                    "from": prior_start.isoformat(), "to": prior_end.isoformat(),
                },
                "privacy": {
                    "protected": True,
                    "excluded_sites": list(INDIVIDUAL_ACTIVITY_PRIVATE_SITES),
                },
                "totals": {
                    "requests": 0, "active_sources": 0, "new_sources": 0,
                    "linked_pages": 0, "sites": 0,
                },
                "series": [], "sources": [], "relationships": [],
                "available_sources": [],
            })

        site_clause = "AND r.site_id=?" if site else ""
        parameters: tuple[Any, ...] = (
            start_text, end_text,
            prior_start.isoformat(), prior_end.isoformat(),
            start_text, start_text, end_text,
            *INDIVIDUAL_ACTIVITY_PRIVATE_SITES,
            *((site["id"],) if site else ()),
        )
        source_rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT s.name site, LOWER(r.referrer_host) source,
                       SUM(CASE WHEN r.day BETWEEN ? AND ?
                           THEN r.human_nonasset_requests ELSE 0 END) requests,
                       SUM(CASE WHEN r.day BETWEEN ? AND ?
                           THEN r.human_nonasset_requests ELSE 0 END) prior_requests,
                       SUM(r.human_nonasset_requests) lifetime_requests,
                       MIN(CASE WHEN r.human_nonasset_requests>0 THEN r.day END) first_seen,
                       MAX(CASE WHEN r.human_nonasset_requests>0 THEN r.day END) last_seen,
                       MAX(CASE WHEN r.day<? AND r.human_nonasset_requests>0
                           THEN r.day END) previous_seen,
                       MIN(CASE WHEN r.day BETWEEN ? AND ?
                                    AND r.human_nonasset_requests>0
                           THEN r.day END) first_current,
                       COUNT(DISTINCT CASE WHEN r.human_nonasset_requests>0
                           THEN r.day END) active_days
                FROM daily_referrer r JOIN sites s ON s.id=r.site_id
                WHERE s.name NOT IN ({private_placeholders}) {site_clause}
                GROUP BY r.site_id, LOWER(r.referrer_host)
                HAVING SUM(r.human_nonasset_requests)>0
                """,
                parameters,
            )
        ]

        available_sources = sorted({row["source"] for row in source_rows})
        if selected_source and selected_source not in available_sources:
            raise ApiError("Unknown referring source", 404)

        for row in source_rows:
            category, explanation = _link_source_category(row, start)
            row["category"] = category
            row["explanation"] = explanation
            row["change"] = row["requests"] - row["prior_requests"]

        visible_sources = [
            row for row in source_rows
            if (row["requests"] or row["prior_requests"])
            and (not selected_source or row["source"] == selected_source)
        ]
        priority = {
            "resurfaced": 0, "debut": 1, "rising": 2, "loyal": 3,
            "one-off": 4, "cooling": 5, "dormant": 6, "steady": 7,
        }
        visible_sources.sort(key=lambda row: (
            priority[row["category"]], -row["requests"], row["site"], row["source"]
        ))

        relationship_site_clause = "AND p.site_id=?" if site else ""
        relationship_source_clause = "AND LOWER(p.referrer_host)=?" if selected_source else ""
        relationship_parameters: tuple[Any, ...] = (
            start_text, end_text, *INDIVIDUAL_ACTIVITY_PRIVATE_SITES,
            *((site["id"],) if site else ()),
            *((selected_source,) if selected_source else ()),
        )
        relationships = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT s.name site, p.path, LOWER(p.referrer_host) source,
                       SUM(p.human_nonasset_requests) requests,
                       MIN(p.day) first_seen, MAX(p.day) last_seen
                FROM daily_page_referrer p JOIN sites s ON s.id=p.site_id
                WHERE p.day BETWEEN ? AND ?
                  AND p.human_nonasset_requests>0
                  AND s.name NOT IN ({private_placeholders})
                  {relationship_site_clause} {relationship_source_clause}
                GROUP BY p.site_id, p.path, LOWER(p.referrer_host)
                ORDER BY requests DESC, s.name, source, p.path
                LIMIT 100
                """,
                relationship_parameters,
            )
        ]

        series_site_clause = "AND r.site_id=?" if site else ""
        series_source_clause = "AND LOWER(r.referrer_host)=?" if selected_source else ""
        series_parameters: tuple[Any, ...] = (
            start_text, end_text, *INDIVIDUAL_ACTIVITY_PRIVATE_SITES,
            *((site["id"],) if site else ()),
            *((selected_source,) if selected_source else ()),
        )
        stored_series = conn.execute(
            f"""
            SELECT r.day bucket, SUM(r.human_nonasset_requests) requests
            FROM daily_referrer r JOIN sites s ON s.id=r.site_id
            WHERE r.day BETWEEN ? AND ? AND r.human_nonasset_requests>0
              AND s.name NOT IN ({private_placeholders})
              {series_site_clause} {series_source_clause}
            GROUP BY r.day ORDER BY r.day
            """,
            series_parameters,
        )
        values = {row["bucket"]: row["requests"] for row in stored_series}
        series = [
            {"bucket": day, "requests": values.get(day, 0)}
            for day in _day_buckets(start_text, end_text)
        ]

    current_sources = [row for row in source_rows if row["requests"]]
    if selected_source:
        current_sources = [
            row for row in current_sources if row["source"] == selected_source
        ]
    totals = {
        "requests": sum(row["requests"] for row in current_sources),
        "active_sources": len({row["source"] for row in current_sources}),
        "new_sources": len({
            (row["site"], row["source"]) for row in current_sources
            if row["category"] == "debut"
        }),
        "linked_pages": len({(row["site"], row["path"]) for row in relationships}),
        "sites": len({row["site"] for row in current_sources}),
    }
    return jsonify({
        "scope": {"site": site["name"] if site else None,
                  "source": selected_source or None},
        "from": start_text, "to": end_text,
        "previous": {"from": prior_start.isoformat(), "to": prior_end.isoformat()},
        "privacy": {
            "protected": False,
            "excluded_sites": list(INDIVIDUAL_ACTIVITY_PRIVATE_SITES),
        },
        "totals": totals,
        "series": series,
        "sources": visible_sources[:limit],
        "relationships": relationships,
        "available_sources": available_sources,
    })


@api_bp.get("/galaxy")
@login_required
def galaxy():
    start, end = _date_range()
    limit = _int_arg("limit", 24, 4, 40)
    private_placeholders = ",".join(
        "?" for _ in INDIVIDUAL_ACTIVITY_PRIVATE_SITES
    )
    empty_totals = {
        "pages": 0, "sources": 0, "ai_agents": 0,
        "mapped_sources": 0, "connections": 0, "page_requests": 0,
    }
    with _conn() as conn:
        site = _requested_site(conn)
        if site and site["name"] in INDIVIDUAL_ACTIVITY_PRIVATE_SITES:
            return jsonify({
                "scope": {"site": site["name"]}, "from": start, "to": end,
                "privacy": {
                    "protected": True,
                    "excluded_sites": list(INDIVIDUAL_ACTIVITY_PRIVATE_SITES),
                },
                "totals": empty_totals, "nodes": [], "edges": [], "pages": [],
            })

        site_clause = "AND p.site_id=?" if site else ""
        page_parameters: tuple[Any, ...] = (
            start, end, *INDIVIDUAL_ACTIVITY_PRIVATE_SITES,
            *((site["id"],) if site else ()), limit,
        )
        pages = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT p.site_id, s.name site, p.path,
                       SUM(p.human_nonasset_requests) requests
                FROM daily_page_status p JOIN sites s ON s.id=p.site_id
                WHERE p.day BETWEEN ? AND ? AND p.status BETWEEN 200 AND 399
                  AND p.human_nonasset_requests>0
                  AND s.name NOT IN ({private_placeholders}) {site_clause}
                GROUP BY p.site_id, p.path
                ORDER BY requests DESC, s.name, p.path LIMIT ?
                """,
                page_parameters,
            )
        ]
        page_ids = {
            (row["site_id"], row["path"]): f"page-{index}"
            for index, row in enumerate(pages)
        }
        page_details = {
            key: {
                **row, "id": page_ids[key], "referrals": 0, "sources": 0,
                "ai_requests": 0, "agents": 0, "entrances": 0, "exits": 0,
                "transitions_in": 0, "transitions_out": 0,
            }
            for key, row in (
                ((row["site_id"], row["path"]), row) for row in pages
            )
        }

        joined_site_clause = "AND x.site_id=?" if site else ""
        common_parameters: tuple[Any, ...] = (
            start, end, *INDIVIDUAL_ACTIVITY_PRIVATE_SITES,
            *((site["id"],) if site else ()),
        )
        referral_rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT x.site_id, s.name site, x.path,
                       LOWER(x.referrer_host) source,
                       SUM(x.human_nonasset_requests) requests
                FROM daily_page_referrer x JOIN sites s ON s.id=x.site_id
                WHERE x.day BETWEEN ? AND ? AND x.human_nonasset_requests>0
                  AND s.name NOT IN ({private_placeholders}) {joined_site_clause}
                GROUP BY x.site_id, x.path, LOWER(x.referrer_host)
                ORDER BY requests DESC, source
                """,
                common_parameters,
            )
        ]
        source_totals: dict[str, int] = defaultdict(int)
        for row in referral_rows:
            if (row["site_id"], row["path"]) in page_ids:
                source_totals[row["source"]] += row["requests"]
        source_limit = min(16, max(8, limit // 2))
        selected_sources = [
            source for source, _ in sorted(
                source_totals.items(), key=lambda item: (-item[1], item[0])
            )[:source_limit]
        ]
        source_ids = {
            source: f"source-{index}" for index, source in enumerate(selected_sources)
        }

        families = tuple(AI_AGENTS)
        family_placeholders = ",".join("?" for _ in families)
        agent_parameters: tuple[Any, ...] = (
            start, end, *families, *INDIVIDUAL_ACTIVITY_PRIVATE_SITES,
            *((site["id"],) if site else ()),
        )
        agent_rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT x.site_id, s.name site, x.path, x.ua_family agent,
                       SUM(x.requests-x.asset_requests) requests
                FROM daily_page_agent x JOIN sites s ON s.id=x.site_id
                WHERE x.day BETWEEN ? AND ? AND x.is_bot=1
                  AND x.ua_family IN ({family_placeholders})
                  AND s.name NOT IN ({private_placeholders}) {joined_site_clause}
                GROUP BY x.site_id, x.path, x.ua_family
                HAVING SUM(x.requests-x.asset_requests)>0
                ORDER BY requests DESC, agent
                """,
                agent_parameters,
            )
        ]
        agent_totals: dict[str, int] = defaultdict(int)
        for row in agent_rows:
            if (row["site_id"], row["path"]) in page_ids:
                agent_totals[row["agent"]] += row["requests"]
        selected_agents = [
            agent for agent, _ in sorted(
                agent_totals.items(), key=lambda item: (-item[1], item[0])
            )[:8]
        ]
        agent_ids = {
            agent: f"agent-{index}" for index, agent in enumerate(selected_agents)
        }

        endpoint_rows = conn.execute(
            f"""
            SELECT x.site_id, x.path, SUM(x.entrances) entrances,
                   SUM(x.exits) exits
            FROM daily_journey_endpoint x JOIN sites s ON s.id=x.site_id
            WHERE x.day BETWEEN ? AND ?
              AND s.name NOT IN ({private_placeholders}) {joined_site_clause}
            GROUP BY x.site_id, x.path
            """,
            common_parameters,
        )
        for row in endpoint_rows:
            key = (row["site_id"], row["path"])
            if key in page_details:
                page_details[key]["entrances"] = row["entrances"]
                page_details[key]["exits"] = row["exits"]

        transition_rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT x.site_id, x.from_path, x.to_path,
                       SUM(x.transitions) transitions
                FROM daily_journey_transition x JOIN sites s ON s.id=x.site_id
                WHERE x.day BETWEEN ? AND ?
                  AND s.name NOT IN ({private_placeholders}) {joined_site_clause}
                GROUP BY x.site_id, x.from_path, x.to_path
                HAVING SUM(x.transitions)>0
                ORDER BY transitions DESC, x.from_path, x.to_path
                """,
                common_parameters,
            )
        ]

    edges = []
    for row in referral_rows:
        page_key = (row["site_id"], row["path"])
        if page_key not in page_ids:
            continue
        details = page_details[page_key]
        details["referrals"] += row["requests"]
        details.setdefault("source_names", set()).add(row["source"])
        if row["source"] in source_ids:
            edges.append({
                "kind": "referral", "from": source_ids[row["source"]],
                "to": page_ids[page_key], "weight": row["requests"],
            })
    for details in page_details.values():
        details["sources"] = len(details.pop("source_names", set()))

    for row in agent_rows:
        page_key = (row["site_id"], row["path"])
        if page_key not in page_ids:
            continue
        details = page_details[page_key]
        details["ai_requests"] += row["requests"]
        details.setdefault("agent_names", set()).add(row["agent"])
        if row["agent"] in agent_ids:
            edges.append({
                "kind": "ai", "from": agent_ids[row["agent"]],
                "to": page_ids[page_key], "weight": row["requests"],
            })
    for details in page_details.values():
        details["agents"] = len(details.pop("agent_names", set()))

    for row in transition_rows:
        from_key = (row["site_id"], row["from_path"])
        to_key = (row["site_id"], row["to_path"])
        if from_key not in page_ids or to_key not in page_ids:
            continue
        edges.append({
            "kind": "journey", "from": page_ids[from_key],
            "to": page_ids[to_key], "weight": row["transitions"],
        })
        page_details[from_key]["transitions_out"] += row["transitions"]
        page_details[to_key]["transitions_in"] += row["transitions"]

    nodes = [
        {
            "id": details["id"], "kind": "page", "label": details["path"],
            "site": details["site"], "path": details["path"],
            "weight": details["requests"],
        }
        for details in page_details.values()
    ]
    nodes.extend({
        "id": source_ids[source], "kind": "source", "label": source,
        "source": source, "weight": source_totals[source],
    } for source in selected_sources)
    for agent in selected_agents:
        provider, purpose = AI_AGENTS[agent]
        nodes.append({
            "id": agent_ids[agent], "kind": "ai", "label": agent,
            "agent": agent, "provider": provider, "purpose": purpose,
            "weight": agent_totals[agent],
        })
    page_output = []
    for details in page_details.values():
        details.pop("site_id", None)
        page_output.append(details)
    return jsonify({
        "scope": {"site": site["name"] if site else None},
        "from": start, "to": end,
        "privacy": {
            "protected": False,
            "excluded_sites": list(INDIVIDUAL_ACTIVITY_PRIVATE_SITES),
        },
        "totals": {
            "pages": len(page_output), "sources": len(source_totals),
            "mapped_sources": len(selected_sources),
            "ai_agents": len(selected_agents), "connections": len(edges),
            "page_requests": sum(row["requests"] for row in page_output),
        },
        "nodes": nodes, "edges": edges, "pages": page_output,
    })


@api_bp.get("/site/<path:name>/countries")
@login_required
def countries(name: str):
    return _ranked_endpoint(name, "daily_country", "country", "countries")


@api_bp.get("/site/<path:name>/status")
@login_required
def status(name: str):
    start, end = _date_range()
    bots, assets = _flags()
    metric = _metric_column(bots, assets)
    with _conn() as conn:
        site = _site(conn, name)
        statuses = [
            dict(row)
            for row in conn.execute(
                f"""SELECT status, SUM({metric}) requests FROM daily_status
                WHERE site_id=? AND day BETWEEN ? AND ? GROUP BY status
                HAVING SUM({metric})>0 ORDER BY status""",
                (site["id"], start, end),
            )
        ]
        missing = [
            dict(row)
            for row in conn.execute(
                f"""SELECT path, SUM({metric}) requests FROM daily_404
                WHERE site_id=? AND day BETWEEN ? AND ? GROUP BY path
                HAVING SUM({metric})>0 ORDER BY requests DESC, path LIMIT 25""",
                (site["id"], start, end),
            )
        ]
    return jsonify({"site": name, "statuses": statuses, "top_404": missing})


@api_bp.get("/site/<path:name>/agents")
@login_required
def agents(name: str):
    start, end = _date_range()
    bots, assets = _flags()
    value = "requests" if assets else "requests-asset_requests"
    with _conn() as conn:
        site = _site(conn, name)
        base_params = (site["id"], start, end, bots)
        browsers = [
            dict(row)
            for row in conn.execute(
                f"""SELECT ua_family, is_bot, SUM({value}) requests FROM daily_agent
                WHERE site_id=? AND day BETWEEN ? AND ? AND (?=1 OR is_bot=0)
                GROUP BY ua_family, is_bot HAVING SUM({value})>0 ORDER BY requests DESC""",
                base_params,
            )
        ]
        operating_systems = [
            dict(row)
            for row in conn.execute(
                f"""SELECT os_family, SUM({value}) requests FROM daily_agent
                WHERE site_id=? AND day BETWEEN ? AND ? AND (?=1 OR is_bot=0)
                GROUP BY os_family HAVING SUM({value})>0 ORDER BY requests DESC""",
                base_params,
            )
        ]
        bot_rows = [
            dict(row)
            for row in conn.execute(
                f"""SELECT ua_family, SUM({value}) requests FROM daily_agent
                WHERE site_id=? AND day BETWEEN ? AND ? AND is_bot=1
                GROUP BY ua_family HAVING SUM({value})>0 ORDER BY requests DESC""",
                (site["id"], start, end),
            )
        ]
    return jsonify({"site": name, "browsers": browsers, "operating_systems": operating_systems, "bots": bot_rows})


@api_bp.get("/live")
@login_required
def live():
    minutes = _int_arg("minutes", 60, 1, 1440)
    limit = _int_arg("limit", 40, 1, 100)
    bots, assets = _flags()
    generated_at = int(datetime.now(timezone.utc).timestamp())
    cutoff = generated_at - minutes * 60
    private_placeholders = ",".join("?" for _ in LIVE_PRIVATE_SITES)
    with _conn() as conn:
        site = _requested_site(conn)
        aggregate_site_clause = "WHERE s.id=?" if site else ""
        request_site_clause = "AND r.site_id=?" if site else ""
        event_site_clause = "AND e.site_id=?" if site else ""
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT s.name site, COUNT(r.id) requests,
                       COUNT(DISTINCT r.ip_hash) unique_visitors,
                       COALESCE(SUM(r.bytes),0) bytes
                FROM sites s LEFT JOIN requests r ON r.site_id=s.id AND r.ts>=?
                  AND (?=1 OR r.is_bot=0) AND (?=1 OR r.is_asset=0)
                {aggregate_site_clause}
                GROUP BY s.id ORDER BY requests DESC, s.name
                """,
                (cutoff, bots, assets, *((site["id"],) if site else ())),
            )
        ]
        recent = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT r.id, r.site_id, s.name site, r.ts, r.day, r.path,
                       r.status, r.ua_family browser, r.os_family,
                       r.country, r.referrer_host
                FROM requests r JOIN sites s ON s.id=r.site_id
                WHERE r.ts>=? AND r.is_bot=0 AND r.is_asset=0
                  AND r.method IN ('GET', 'HEAD')
                  AND r.status BETWEEN 200 AND 399
                  AND s.name NOT IN ({private_placeholders})
                  {request_site_clause}
                ORDER BY r.ts DESC, r.id DESC LIMIT ?
                """,
                (
                    cutoff, *LIVE_PRIVATE_SITES,
                    *((site["id"],) if site else ()), limit,
                ),
            )
        ]
        exact_moments: dict[tuple[int, int, str], list[str]] = defaultdict(list)
        for row in conn.execute(
            f"""
            SELECT e.site_id, e.occurred_at, e.path, e.kind
            FROM events e JOIN sites s ON s.id=e.site_id
            WHERE e.occurred_at>=? AND e.path IS NOT NULL
              AND e.kind IN ('new_page', 'new_referrer', 'content_resurfaced')
              AND s.name NOT IN ({private_placeholders})
              {event_site_clause}
            ORDER BY e.occurred_at
            """,
            (cutoff, *LIVE_PRIVATE_SITES, *((site["id"],) if site else ())),
        ):
            exact_moments[(row["site_id"], row["occurred_at"], row["path"])].append(
                row["kind"]
            )
        daily_moments: dict[tuple[int, str], list[str]] = defaultdict(list)
        activity_days = sorted({row["day"] for row in recent})
        if activity_days:
            day_placeholders = ",".join("?" for _ in activity_days)
            for row in conn.execute(
                f"""
                SELECT e.site_id, e.day, e.kind
                FROM events e JOIN sites s ON s.id=e.site_id
                WHERE e.day IN ({day_placeholders})
                  AND e.kind IN (
                      'traffic_record', 'traffic_spike', 'visitor_milestone'
                  )
                  AND s.name NOT IN ({private_placeholders})
                  {event_site_clause}
                ORDER BY e.occurred_at, e.id
                """,
                (
                    *activity_days, *LIVE_PRIVATE_SITES,
                    *((site["id"],) if site else ()),
                ),
            ):
                daily_moments[(row["site_id"], row["day"])].append(row["kind"])
        activity = []
        assigned_daily_moments: set[tuple[int, str]] = set()
        for row in recent:
            item = dict(row)
            item.pop("id")
            day_key = (row["site_id"], row["day"])
            moments = list(exact_moments.get(
                (row["site_id"], row["ts"], row["path"]), []
            ))
            if day_key not in assigned_daily_moments:
                moments.extend(daily_moments.get(day_key, []))
                assigned_daily_moments.add(day_key)
            item["moments"] = moments
            item.pop("site_id")
            activity.append(item)
        country_rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT r.country, COUNT(*) requests,
                       COUNT(DISTINCT r.ip_hash) visitors
                FROM requests r JOIN sites s ON s.id=r.site_id
                WHERE r.ts>=? AND r.is_bot=0 AND r.is_asset=0
                  AND r.method IN ('GET', 'HEAD')
                  AND r.status BETWEEN 200 AND 399
                  AND r.country IS NOT NULL
                  AND s.name NOT IN ({private_placeholders})
                  {request_site_clause}
                GROUP BY r.country ORDER BY requests DESC, r.country
                """,
                (cutoff, *LIVE_PRIVATE_SITES, *((site["id"],) if site else ())),
            )
        ]
        radar_totals = dict(
            conn.execute(
                f"""
                SELECT COUNT(*) requests, COUNT(DISTINCT r.ip_hash) visitors,
                       COUNT(DISTINCT r.country) countries,
                       COUNT(DISTINCT r.site_id) sites
                FROM requests r JOIN sites s ON s.id=r.site_id
                WHERE r.ts>=? AND r.is_bot=0 AND r.is_asset=0
                  AND r.method IN ('GET', 'HEAD')
                  AND r.status BETWEEN 200 AND 399
                  AND s.name NOT IN ({private_placeholders})
                  {request_site_clause}
                """,
                (cutoff, *LIVE_PRIVATE_SITES, *((site["id"],) if site else ())),
            ).fetchone()
        )
    return jsonify({
        "minutes": minutes,
        "sites": rows,
        "activity": activity,
        "countries": country_rows,
        "totals": radar_totals,
        "generated_at": generated_at,
        "privacy": {"excluded_activity_sites": list(LIVE_PRIVATE_SITES)},
        "scope": {"site": site["name"] if site else None},
    })
