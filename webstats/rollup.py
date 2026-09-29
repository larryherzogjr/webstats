"""Daily aggregate maintenance and raw-row retention."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from .bots import AI_AGENTS


def maintain_rollups(
    conn: sqlite3.Connection,
    retention_days: int,
    affected_days: Optional[Iterable[str]] = None,
    timezone_name: str = "UTC",
) -> None:
    today = datetime.now(ZoneInfo(timezone_name)).date()
    if affected_days is None:
        days = {row[0] for row in conn.execute("SELECT DISTINCT day FROM requests")}
    else:
        days = set(affected_days)
        days.update({today.isoformat(), (today - timedelta(days=1)).isoformat()})
    for day in days:
        recompute_day(conn, day)
    cutoff = (today - timedelta(days=retention_days)).isoformat()
    conn.execute("DELETE FROM requests WHERE day < ?", (cutoff,))
    conn.commit()


def recompute_day(conn: sqlite3.Connection, day: str) -> None:
    tables = (
        "daily_site", "daily_traffic", "daily_filter", "daily_path",
        "daily_referrer", "daily_page_referrer", "daily_agent",
        "daily_page_agent", "daily_country", "daily_status", "daily_404",
    )
    for table in tables:
        conn.execute(f"DELETE FROM {table} WHERE day = ?", (day,))
    conn.execute(
        """
        INSERT INTO daily_site
        SELECT site_id, day, COUNT(*), SUM(is_bot=0), SUM(is_bot=1),
               COUNT(DISTINCT CASE WHEN is_bot=0 THEN ip_hash END), SUM(bytes),
               SUM(status BETWEEN 200 AND 299), SUM(status BETWEEN 300 AND 399),
               SUM(status BETWEEN 400 AND 499), SUM(status BETWEEN 500 AND 599)
        FROM requests WHERE day=? GROUP BY site_id, day
        """,
        (day,),
    )
    for include_bots in (0, 1):
        for include_assets in (0, 1):
            clauses = ["day=?"]
            if not include_bots:
                clauses.append("is_bot=0")
            if not include_assets:
                clauses.append("is_asset=0")
            where = " AND ".join(clauses)
            conn.execute(
                f"""
                INSERT INTO daily_filter
                SELECT site_id, day, ?, ?, COUNT(*), COUNT(DISTINCT ip_hash),
                       SUM(bytes), SUM(status BETWEEN 200 AND 299),
                       SUM(status BETWEEN 300 AND 399),
                       SUM(status BETWEEN 400 AND 499),
                       SUM(status BETWEEN 500 AND 599)
                FROM requests WHERE {where} GROUP BY site_id, day
                """,
                (include_bots, include_assets, day),
            )
    conn.execute(
        """
        INSERT INTO daily_traffic
        SELECT site_id, day, is_bot, is_asset, COUNT(*), COUNT(DISTINCT ip_hash),
               SUM(bytes), SUM(status BETWEEN 200 AND 299),
               SUM(status BETWEEN 300 AND 399), SUM(status BETWEEN 400 AND 499),
               SUM(status BETWEEN 500 AND 599)
        FROM requests WHERE day=? GROUP BY site_id, day, is_bot, is_asset
        """,
        (day,),
    )
    conn.execute(
        """
        INSERT INTO daily_404
        SELECT site_id, day, path, COUNT(*), SUM(is_bot=0), SUM(is_asset=0),
               SUM(is_bot=0 AND is_asset=0)
        FROM requests WHERE day=? AND status=404 GROUP BY site_id, day, path
        """,
        (day,),
    )
    conn.execute(
        """
        INSERT INTO daily_path
        SELECT site_id, day, path, COUNT(*), COUNT(DISTINCT ip_hash),
               SUM(is_bot=0), COUNT(DISTINCT CASE WHEN is_bot=0 THEN ip_hash END),
               SUM(is_asset=1)
        FROM requests WHERE day=? GROUP BY site_id, day, path
        """,
        (day,),
    )
    conn.execute(
        """
        INSERT INTO daily_referrer
        SELECT site_id, day, referrer_host, COUNT(*), SUM(is_bot=0),
               SUM(is_asset=0), SUM(is_bot=0 AND is_asset=0)
        FROM requests WHERE day=? AND referrer_host IS NOT NULL
        GROUP BY site_id, day, referrer_host
        """,
        (day,),
    )
    conn.execute(
        """
        INSERT INTO daily_page_referrer
        SELECT site_id, day, path, referrer_host, COUNT(*), SUM(is_bot=0),
               SUM(is_asset=0), SUM(is_bot=0 AND is_asset=0)
        FROM requests WHERE day=? AND referrer_host IS NOT NULL
        GROUP BY site_id, day, path, referrer_host
        """,
        (day,),
    )
    conn.execute(
        """
        INSERT INTO daily_agent
        SELECT site_id, day, ua_family, os_family, is_bot, COUNT(*), SUM(is_asset=1)
        FROM requests WHERE day=? GROUP BY site_id, day, ua_family, os_family, is_bot
        """,
        (day,),
    )
    conn.execute(
        """
        INSERT INTO daily_page_agent
        SELECT site_id, day, path, ua_family, is_bot, COUNT(*), SUM(is_asset=1)
        FROM requests WHERE day=?
        GROUP BY site_id, day, path, ua_family, is_bot
        """,
        (day,),
    )
    conn.execute(
        """
        INSERT INTO daily_country
        SELECT site_id, day, country, COUNT(*), SUM(is_bot=0),
               SUM(is_asset=0), SUM(is_bot=0 AND is_asset=0)
        FROM requests WHERE day=? AND country IS NOT NULL
        GROUP BY site_id, day, country
        """,
        (day,),
    )
    conn.execute(
        """
        INSERT INTO daily_status
        SELECT site_id, day, status, COUNT(*), SUM(is_bot=0),
               SUM(is_asset=0), SUM(is_bot=0 AND is_asset=0)
        FROM requests WHERE day=? GROUP BY site_id, day, status
        """,
        (day,),
    )
    _refresh_events_for_day(conn, day)


def _upsert_event(conn: sqlite3.Connection, row: sqlite3.Row, kind: str) -> None:
    subject = row["referrer_host"] if kind == "new_referrer" else row["ua_family"]
    conn.execute(
        """
        INSERT INTO events(
            site_id, kind, event_key, occurred_at, day, path,
            source, agent, country
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_key) DO UPDATE SET
            occurred_at=excluded.occurred_at,
            day=excluded.day,
            path=excluded.path,
            source=excluded.source,
            agent=excluded.agent,
            country=excluded.country
        WHERE excluded.occurred_at < events.occurred_at
        """,
        (
            row["site_id"],
            kind,
            f"{kind}:{row['site_id']}:{subject}",
            row["ts"],
            row["day"],
            row["path"],
            row["referrer_host"] if kind == "new_referrer" else None,
            row["ua_family"] if kind == "first_ai_visit" else None,
            row["country"],
        ),
    )


def _refresh_events_for_day(conn: sqlite3.Connection, day: str) -> None:
    referrers = conn.execute(
        """
        SELECT site_id, ts, day, path, referrer_host, NULL ua_family, country
        FROM (
            SELECT site_id, ts, day, path, referrer_host, country,
                   ROW_NUMBER() OVER (
                       PARTITION BY site_id, referrer_host ORDER BY ts, id
                   ) position
            FROM requests
            WHERE day=? AND referrer_host IS NOT NULL
              AND is_bot=0 AND is_asset=0
        ) WHERE position=1
        """,
        (day,),
    )
    for row in referrers:
        _upsert_event(conn, row, "new_referrer")

    families = tuple(AI_AGENTS)
    placeholders = ",".join("?" for _ in families)
    crawlers = conn.execute(
        f"""
        SELECT site_id, ts, day, path, NULL referrer_host, ua_family, country
        FROM (
            SELECT site_id, ts, day, path, ua_family, country,
                   ROW_NUMBER() OVER (
                       PARTITION BY site_id, ua_family ORDER BY ts, id
                   ) position
            FROM requests
            WHERE day=? AND is_bot=1 AND is_asset=0
              AND ua_family IN ({placeholders})
        ) WHERE position=1
        """,
        (day, *families),
    )
    for row in crawlers:
        _upsert_event(conn, row, "first_ai_visit")
