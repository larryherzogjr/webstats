"""JSON API for dashboard pages."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
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


api_bp = Blueprint("api", __name__, url_prefix="/api")


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


def _metric_column(bots: int, assets: int) -> str:
    if bots and assets:
        return "requests"
    if bots:
        return "nonasset_requests"
    if assets:
        return "human_requests"
    return "human_nonasset_requests"


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
    with _conn() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT e.kind, e.occurred_at, e.day, e.path, e.source,
                       e.agent, e.country, e.value, s.name site
                FROM events e JOIN sites s ON s.id=e.site_id
                WHERE e.day BETWEEN ? AND ?
                ORDER BY e.occurred_at DESC, e.id DESC LIMIT ?
                """,
                (start, end, limit),
            )
        ]
    return jsonify({"from": start, "to": end, "events": rows})


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
              AND is_bot=1 AND {value} > 0
            """,
            (start, end, *families),
        ).fetchone()
        stored = conn.execute(
            f"""
            SELECT s.name site, p.ua_family agent, p.path,
                   SUM({value}) requests, MIN(p.day) first_seen,
                   MAX(p.day) last_seen
            FROM daily_page_agent p JOIN sites s ON s.id=p.site_id
            WHERE p.day BETWEEN ? AND ? AND p.is_bot=1
              AND p.ua_family IN ({placeholders})
            GROUP BY p.site_id, p.ua_family, p.path
            HAVING SUM({value}) > 0
            ORDER BY requests DESC, last_seen DESC, site, agent, path
            LIMIT ?
            """,
            (start, end, *families, limit),
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
    })


@api_bp.get("/feed-readers")
@login_required
def feed_readers():
    start, end = _date_range()
    limit = _int_arg("limit", 250, 1, 500)
    with _conn() as conn:
        stored = [
            dict(row)
            for row in conn.execute(
                """
                SELECT f.day, s.name site, f.path, f.reader, f.requests,
                       f.reported_subscribers, f.first_seen, f.last_seen
                FROM daily_feed_reader f JOIN sites s ON s.id=f.site_id
                WHERE f.day BETWEEN ? AND ?
                ORDER BY f.day, f.last_seen
                """,
                (start, end),
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
            SELECT referrer_host, SUM({metric}) requests
            FROM daily_page_referrer
            WHERE site_id=? AND path=? AND day BETWEEN ? AND ?
            GROUP BY referrer_host HAVING SUM({metric})>0
            """,
            (site["id"], path, start, end),
        )
        grouped_referrers: dict[str, int] = defaultdict(int)
        for row in referrer_rows:
            grouped_referrers[_referrer_group(row["referrer_host"])] += row["requests"]
        referrers = [
            {"group": group, "requests": count}
            for group, count in sorted(
                grouped_referrers.items(), key=lambda item: (-item[1], item[0])
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
    bots, assets = _flags()
    cutoff = int(datetime.now(timezone.utc).timestamp()) - minutes * 60
    with _conn() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT s.name site, COUNT(r.id) requests,
                       COUNT(DISTINCT r.ip_hash) unique_visitors,
                       COALESCE(SUM(r.bytes),0) bytes
                FROM sites s LEFT JOIN requests r ON r.site_id=s.id AND r.ts>=?
                  AND (?=1 OR r.is_bot=0) AND (?=1 OR r.is_asset=0)
                GROUP BY s.id ORDER BY requests DESC, s.name
                """,
                (cutoff, bots, assets),
            )
        ]
    return jsonify({"minutes": minutes, "sites": rows, "generated_at": int(datetime.now(timezone.utc).timestamp())})
