"""Comparable traffic windows and explainable investigation leads."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo


BASELINE_WEEKS = 4


def _bounds(day: date, interval: str, zone: ZoneInfo, hour: int = 0) -> tuple[int, int]:
    start = datetime.combine(day, time(hour=hour), tzinfo=zone)
    if interval == "hour":
        return int(start.timestamp()), int(start.timestamp()) + 3600
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return int(start.timestamp()), int(end.timestamp())


def _raw_where() -> str:
    return "site_id=? AND ts>=? AND ts<? AND (?=1 OR is_bot=0) AND (?=1 OR is_asset=0)"


def _request_count(
    conn: sqlite3.Connection, site_id: int, start: int, end: int,
    bots: int, assets: int,
) -> int:
    return int(conn.execute(
        f"SELECT COUNT(*) FROM requests WHERE {_raw_where()}",
        (site_id, start, end, bots, assets),
    ).fetchone()[0])


def comparable_periods(
    conn: sqlite3.Connection, site_id: int, day: date, interval: str,
    hour: int, zone: ZoneInfo, cutoff: date,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Use the same weekday, and clock hour when requested, over four weeks."""
    candidates = [day - timedelta(weeks=week) for week in range(1, BASELINE_WEEKS + 1)]
    earliest = min(candidates).isoformat()
    latest = max(candidates).isoformat()
    ledger_days = {
        row["day"] for row in conn.execute(
            "SELECT day FROM rollup_days WHERE day BETWEEN ? AND ?", (earliest, latest)
        )
    }
    site_days = {
        row["day"] for row in conn.execute(
            "SELECT DISTINCT day FROM requests WHERE site_id=? AND day BETWEEN ? AND ?",
            (site_id, earliest, latest),
        )
    }
    periods: list[dict[str, Any]] = []
    missing: list[str] = []
    for prior_day in candidates:
        label = prior_day.isoformat()
        if prior_day < cutoff or (label not in ledger_days and label not in site_days):
            missing.append(label)
            continue
        start, end = _bounds(prior_day, interval, zone, hour)
        periods.append({
            "day": label, "start": start, "end": end,
            "site_activity": label in site_days,
        })
    return periods, missing


def _dimension_counts(
    conn: sqlite3.Connection, site_id: int, start: int, end: int,
    bots: int, assets: int, dimension: str,
) -> Counter[str]:
    expressions = {
        "pages": "path",
        "referrers": "COALESCE(referrer_host, '[no external referrer]')",
        "agents": "ua_family",
    }
    expression = expressions[dimension]
    bot_clause = " AND is_bot=1" if dimension == "agents" else ""
    return Counter({
        row["label"]: row["requests"] for row in conn.execute(
            f"""SELECT {expression} label, COUNT(*) requests FROM requests
                WHERE {_raw_where()}{bot_clause} GROUP BY label""",
            (site_id, start, end, bots, assets),
        )
    })


def contributor_changes(
    conn: sqlite3.Connection, site_id: int, start: int, end: int,
    periods: list[dict[str, Any]], bots: int, assets: int,
) -> dict[str, list[dict[str, Any]]]:
    result = {}
    for dimension in ("pages", "referrers", "agents"):
        current = _dimension_counts(conn, site_id, start, end, bots, assets, dimension)
        history = [
            _dimension_counts(conn, site_id, item["start"], item["end"], bots, assets, dimension)
            for item in periods
        ]
        keys = set(current)
        for sample in history:
            keys.update(sample)
        rows = []
        for label in keys:
            baseline = round(sum(sample[label] for sample in history) / len(history), 1) if history else None
            change = round(current[label] - baseline, 1) if baseline is not None else None
            rows.append({
                "label": label, "requests": current[label],
                "baseline": baseline, "change": change,
            })
        rows.sort(key=lambda item: (
            -abs(item["change"] if item["change"] is not None else item["requests"]),
            -item["requests"], item["label"],
        ))
        result[dimension] = rows[:5]
    return result


def chart_leads(
    conn: sqlite3.Connection, site_id: int, rows: list[dict[str, Any]],
    interval: str, zone: ZoneInfo, cutoff: date, now: datetime,
    bots: int, assets: int,
) -> list[dict[str, Any]]:
    """Flag completed chart buckets with enough same-weekday evidence."""
    candidates = [
        row for row in rows
        if row["requests"] >= 10 and date.fromisoformat(row["bucket"][:10]) >= cutoff
    ]
    if not candidates:
        return []
    first = min(date.fromisoformat(row["bucket"][:10]) for row in candidates) - timedelta(weeks=BASELINE_WEEKS)
    last = max(date.fromisoformat(row["bucket"][:10]) for row in candidates)
    ledger = {
        row["day"] for row in conn.execute(
            "SELECT day FROM rollup_days WHERE day BETWEEN ? AND ?",
            (first.isoformat(), last.isoformat()),
        )
    }
    site_days = {
        row["day"] for row in conn.execute(
            "SELECT DISTINCT day FROM requests WHERE site_id=? AND day BETWEEN ? AND ?",
            (site_id, first.isoformat(), last.isoformat()),
        )
    }
    counts: dict[tuple[str, int], int] = {}
    if interval == "day":
        counts = {
            (item["day"], 0): item["requests"] for item in conn.execute(
                """SELECT day, requests FROM daily_filter WHERE site_id=?
                   AND include_bots=? AND include_assets=? AND day BETWEEN ? AND ?""",
                (site_id, bots, assets, first.isoformat(), last.isoformat()),
            )
        }
    else:
        grouped: Counter[tuple[str, int]] = Counter()
        for item in conn.execute(
            f"""SELECT ts FROM requests WHERE site_id=? AND day BETWEEN ? AND ?
                AND (?=1 OR is_bot=0) AND (?=1 OR is_asset=0)""",
            (site_id, first.isoformat(), last.isoformat(), bots, assets),
        ):
            local = datetime.fromtimestamp(item["ts"], zone)
            grouped[(local.date().isoformat(), local.hour)] += 1
        counts = dict(grouped)
    leads = []
    for row in candidates:
        day = date.fromisoformat(row["bucket"][:10])
        hour = int(row["bucket"][11:13]) if interval == "hour" else 0
        if day < cutoff or (interval == "day" and day >= now.date()):
            continue
        if interval == "hour" and datetime.strptime(
            row["bucket"], "%Y-%m-%dT%H:%M:%S%z"
        ).timestamp() + 3600 > now.timestamp():
            continue
        samples = []
        active_samples = 0
        for week in range(1, BASELINE_WEEKS + 1):
            prior = day - timedelta(weeks=week)
            label = prior.isoformat()
            if prior >= cutoff and (label in ledger or label in site_days):
                samples.append(counts.get((label, hour), 0))
                active_samples += label in site_days
        if len(samples) < 3 or active_samples < 2:
            continue
        baseline = sum(samples) / len(samples)
        excess = row["requests"] - baseline
        if excess < 8 or row["requests"] < max(10, baseline * 2):
            continue
        leads.append({
            "bucket": row["bucket"], "requests": row["requests"],
            "baseline": round(baseline, 1), "excess": round(excess, 1),
            "samples": len(samples),
        })
    return sorted(leads, key=lambda item: (-item["excess"], item["bucket"]))[:8]
