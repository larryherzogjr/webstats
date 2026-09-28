"""Daily aggregate maintenance and raw-row retention."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta


def maintain_rollups(conn: sqlite3.Connection, retention_days: int) -> None:
    days = [row[0] for row in conn.execute("SELECT DISTINCT day FROM requests")]
    for day in days:
        recompute_day(conn, day)
    cutoff = (date.today() - timedelta(days=retention_days)).isoformat()
    conn.execute("DELETE FROM requests WHERE day < ?", (cutoff,))
    conn.commit()


def recompute_day(conn: sqlite3.Connection, day: str) -> None:
    tables = (
        "daily_site", "daily_path", "daily_referrer", "daily_agent",
        "daily_country", "daily_status",
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
        SELECT site_id, day, referrer_host, COUNT(*), SUM(is_bot=0)
        FROM requests WHERE day=? AND referrer_host IS NOT NULL
        GROUP BY site_id, day, referrer_host
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
        INSERT INTO daily_country
        SELECT site_id, day, country, COUNT(*), SUM(is_bot=0)
        FROM requests WHERE day=? AND country IS NOT NULL
        GROUP BY site_id, day, country
        """,
        (day,),
    )
    conn.execute(
        """
        INSERT INTO daily_status
        SELECT site_id, day, status, COUNT(*), SUM(is_bot=0)
        FROM requests WHERE day=? GROUP BY site_id, day, status
        """,
        (day,),
    )

