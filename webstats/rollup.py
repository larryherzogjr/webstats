"""Daily aggregate maintenance and raw-row retention."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from .bots import AI_AGENTS, classify_feed_reader
from .privacy import INDIVIDUAL_ACTIVITY_PRIVATE_SITES


VISITOR_MILESTONES = (
    100, 250, 500, 1_000, 2_500, 5_000, 10_000, 25_000, 50_000,
    100_000, 250_000, 500_000, 1_000_000,
)
SUBSCRIBER_MILESTONES = (
    10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000,
)
SUMMARY_EVENT_KINDS = (
    "traffic_record", "traffic_spike", "visitor_milestone",
    "feed_subscriber_milestone",
)
PROBE_PATH_PREFIXES = (
    "/.env", "/.git", "/.svn", "/wp-admin", "/wp-content",
    "/wp-includes", "/vendor/phpunit", "/cgi-bin/", "/actuator",
    "/_profiler", "/server-status", "/phpmyadmin", "/boaform",
)
PROBE_PATHS = {"/wp-login.php", "/xmlrpc.php", "/phpinfo.php"}


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
    _refresh_summary_moments(conn, timezone_name)
    cutoff = (today - timedelta(days=retention_days)).isoformat()
    conn.execute("DELETE FROM requests WHERE day < ?", (cutoff,))
    conn.commit()


def recompute_day(conn: sqlite3.Connection, day: str) -> None:
    tables = (
        "daily_site", "daily_traffic", "daily_filter", "daily_path",
        "daily_referrer", "daily_page_referrer", "daily_agent",
        "daily_page_agent", "daily_page_country", "daily_page_status",
        "daily_feed_reader", "daily_country", "daily_status", "daily_404",
        "daily_journey", "daily_journey_endpoint", "daily_journey_transition",
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
        INSERT INTO daily_page_country
        SELECT site_id, day, path, country, COUNT(*), SUM(is_bot=0),
               SUM(is_asset=0), SUM(is_bot=0 AND is_asset=0)
        FROM requests WHERE day=? AND country IS NOT NULL
        GROUP BY site_id, day, path, country
        """,
        (day,),
    )
    conn.execute(
        """
        INSERT INTO daily_page_status
        SELECT site_id, day, path, status, COUNT(*), SUM(is_bot=0),
               SUM(is_asset=0), SUM(is_bot=0 AND is_asset=0)
        FROM requests WHERE day=?
        GROUP BY site_id, day, path, status
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
    _refresh_feed_readers_for_day(conn, day)
    _refresh_journeys_for_day(conn, day)
    _refresh_events_for_day(conn, day)


def _refresh_journeys_for_day(conn: sqlite3.Connection, day: str) -> None:
    """Build anonymous 30-minute visit aggregates without retaining sessions."""
    private = set(INDIVIDUAL_ACTIVITY_PRIVATE_SITES)
    rows = conn.execute(
        """
        SELECT r.site_id, s.name site, r.ip_hash, r.ts, r.id, r.path
        FROM requests r JOIN sites s ON s.id=r.site_id
        WHERE r.day=? AND r.is_bot=0 AND r.is_asset=0
          AND r.method='GET' AND r.status BETWEEN 200 AND 399
        ORDER BY r.site_id, r.ip_hash, r.ts, r.id
        """,
        (day,),
    )
    summaries: dict[int, dict[str, int]] = defaultdict(
        lambda: {
            "sessions": 0, "pageviews": 0, "path_steps": 0,
            "single_page_sessions": 0, "multi_page_sessions": 0,
            "max_depth": 0, "duration_seconds": 0,
        }
    )
    endpoints: dict[tuple[int, str], list[int]] = defaultdict(
        lambda: [0, 0, 0]
    )
    transitions: dict[tuple[int, str, str], int] = defaultdict(int)

    current_key: Optional[tuple[int, str]] = None
    paths: list[str] = []
    pageviews = 0
    first_ts = 0
    last_ts = 0

    def flush() -> None:
        nonlocal paths, pageviews, first_ts, last_ts
        if current_key is None or not paths:
            return
        site_id = current_key[0]
        summary = summaries[site_id]
        depth = len(paths)
        summary["sessions"] += 1
        summary["pageviews"] += pageviews
        summary["path_steps"] += depth
        summary["max_depth"] = max(summary["max_depth"], depth)
        summary["duration_seconds"] += max(0, last_ts - first_ts)
        is_single = int(depth == 1)
        summary["single_page_sessions"] += is_single
        summary["multi_page_sessions"] += int(depth > 1)
        endpoints[(site_id, paths[0])][0] += 1
        endpoints[(site_id, paths[-1])][1] += 1
        if is_single:
            endpoints[(site_id, paths[0])][2] += 1
        for from_path, to_path in zip(paths, paths[1:]):
            transitions[(site_id, from_path, to_path)] += 1

    for row in rows:
        if row["site"] in private:
            continue
        key = (row["site_id"], row["ip_hash"])
        new_session = key != current_key or (last_ts and row["ts"] - last_ts > 1800)
        if new_session:
            flush()
            current_key = key
            paths = []
            pageviews = 0
            first_ts = row["ts"]
        pageviews += 1
        last_ts = row["ts"]
        if not paths or paths[-1] != row["path"]:
            paths.append(row["path"])
    flush()

    conn.executemany(
        """
        INSERT INTO daily_journey(
            site_id, day, sessions, pageviews, path_steps,
            single_page_sessions, multi_page_sessions, max_depth,
            duration_seconds
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                site_id, day, values["sessions"], values["pageviews"],
                values["path_steps"], values["single_page_sessions"],
                values["multi_page_sessions"], values["max_depth"],
                values["duration_seconds"],
            )
            for site_id, values in summaries.items()
        ),
    )
    conn.executemany(
        """
        INSERT INTO daily_journey_endpoint(
            site_id, day, path, entrances, exits, single_page_sessions
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            (site_id, day, path, *values)
            for (site_id, path), values in endpoints.items()
        ),
    )
    conn.executemany(
        """
        INSERT INTO daily_journey_transition(
            site_id, day, from_path, to_path, transitions
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            (site_id, day, from_path, to_path, count)
            for (site_id, from_path, to_path), count in transitions.items()
        ),
    )


def _refresh_feed_readers_for_day(conn: sqlite3.Connection, day: str) -> None:
    observations: dict[tuple[int, str, str, str], dict[str, int | None]] = {}
    for row in conn.execute(
        """
        SELECT site_id, day, path, user_agent, ts
        FROM requests WHERE day=? AND is_asset=0 ORDER BY ts, id
        """,
        (day,),
    ):
        classified = classify_feed_reader(row["user_agent"])
        if classified is None:
            continue
        reader, subscribers = classified
        key = (row["site_id"], row["day"], row["path"], reader)
        item = observations.setdefault(
            key,
            {
                "requests": 0,
                "reported_subscribers": None,
                "first_seen": row["ts"],
                "last_seen": row["ts"],
            },
        )
        item["requests"] = int(item["requests"] or 0) + 1
        item["last_seen"] = row["ts"]
        if subscribers is not None:
            item["reported_subscribers"] = subscribers

    conn.executemany(
        """
        INSERT INTO daily_feed_reader(
            site_id, day, path, reader, requests, reported_subscribers,
            first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (*key, values["requests"], values["reported_subscribers"],
             values["first_seen"], values["last_seen"])
            for key, values in observations.items()
        ),
    )


def _upsert_event(conn: sqlite3.Connection, row: sqlite3.Row, kind: str) -> None:
    if kind == "new_referrer":
        subject = row["referrer_host"]
    elif kind == "first_ai_visit":
        subject = row["ua_family"]
    else:
        subject = row["path"]
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

    pages = conn.execute(
        """
        SELECT site_id, ts, day, path, NULL referrer_host,
               NULL ua_family, country
        FROM (
            SELECT r.site_id, r.ts, r.day, r.path, r.country,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.site_id, r.path ORDER BY r.ts, r.id
                   ) position
            FROM requests r
            WHERE r.day=? AND r.is_bot=0 AND r.is_asset=0
              AND r.method IN ('GET', 'HEAD') AND r.status BETWEEN 200 AND 399
              AND NOT EXISTS (
                  SELECT 1 FROM daily_page_status old
                  WHERE old.site_id=r.site_id AND old.path=r.path
                    AND old.day < r.day AND old.status BETWEEN 200 AND 399
                    AND old.human_nonasset_requests > 0
              )
        ) WHERE position=1
        """,
        (day,),
    )
    for row in pages:
        if _is_meaningful_page(row["path"]):
            _upsert_event(conn, row, "new_page")

    resurfaced = conn.execute(
        """
        SELECT r.site_id, MIN(r.ts) occurred_at, r.day, r.path,
               MAX(old.day) previous_seen
        FROM requests r JOIN daily_page_status old
          ON old.site_id=r.site_id AND old.path=r.path AND old.day<r.day
         AND old.status BETWEEN 200 AND 399
         AND old.human_nonasset_requests>0
        WHERE r.day=? AND r.is_bot=0 AND r.is_asset=0
          AND r.method IN ('GET', 'HEAD') AND r.status BETWEEN 200 AND 399
        GROUP BY r.site_id, r.day, r.path
        HAVING MAX(old.day) <= date(?, '-30 days')
        """,
        (day, day),
    )
    for row in resurfaced:
        if not _is_meaningful_page(row["path"]):
            continue
        conn.execute(
            """
            INSERT INTO events(
                site_id, kind, event_key, occurred_at, day, path, source
            ) VALUES (?, 'content_resurfaced', ?, ?, ?, ?, ?)
            ON CONFLICT(event_key) DO UPDATE SET
                occurred_at=excluded.occurred_at,
                source=excluded.source
            """,
            (
                row["site_id"],
                f"content_resurfaced:{row['site_id']}:{row['path']}:{row['day']}",
                row["occurred_at"], row["day"], row["path"], row["previous_seen"],
            ),
        )


def _is_meaningful_page(path: str) -> bool:
    lowered = path.lower()
    return (
        path != "/"
        and lowered not in PROBE_PATHS
        and not any(lowered.startswith(prefix) for prefix in PROBE_PATH_PREFIXES)
    )


def _summary_timestamp(day: str, timezone_name: str) -> int:
    local_noon = datetime.fromisoformat(f"{day}T12:00:00").replace(
        tzinfo=ZoneInfo(timezone_name)
    )
    return int(local_noon.timestamp())


def _insert_summary_event(
    conn: sqlite3.Connection,
    *,
    site_id: int,
    kind: str,
    subject: str,
    day: str,
    timezone_name: str,
    value: int,
    path: Optional[str] = None,
    source: Optional[str] = None,
    occurred_at: Optional[int] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO events(
            site_id, kind, event_key, occurred_at, day, path, source, value
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            site_id,
            kind,
            f"{kind}:{site_id}:{subject}",
            occurred_at or _summary_timestamp(day, timezone_name),
            day,
            path,
            source,
            value,
        ),
    )


def _refresh_summary_moments(
    conn: sqlite3.Connection, timezone_name: str = "UTC"
) -> None:
    """Rebuild deterministic moments derived from permanent daily rollups."""
    placeholders = ",".join("?" for _ in SUMMARY_EVENT_KINDS)
    conn.execute(
        f"DELETE FROM events WHERE kind IN ({placeholders})",
        SUMMARY_EVENT_KINDS,
    )
    by_site: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT site_id, day, requests, unique_visitors
        FROM daily_filter
        WHERE include_bots=0 AND include_assets=0 AND requests>0
        ORDER BY site_id, day
        """
    ):
        by_site[row["site_id"]].append(row)

    for site_id, rows in by_site.items():
        record = rows[0]["requests"]
        cumulative = 0
        first_day = datetime.fromisoformat(rows[0]["day"]).date()
        daily_requests = {row["day"]: row["requests"] for row in rows}
        for index, row in enumerate(rows):
            current_day = datetime.fromisoformat(row["day"]).date()
            is_record = index > 0 and row["requests"] > record
            if is_record and row["requests"] >= 10:
                _insert_summary_event(
                    conn,
                    site_id=site_id,
                    kind="traffic_record",
                    subject=row["day"],
                    day=row["day"],
                    timezone_name=timezone_name,
                    value=row["requests"],
                )
            record = max(record, row["requests"])

            previous_total = cumulative
            cumulative += row["unique_visitors"]
            crossed = [
                target for target in VISITOR_MILESTONES
                if previous_total < target <= cumulative
            ]
            if crossed:
                target = crossed[-1]
                _insert_summary_event(
                    conn,
                    site_id=site_id,
                    kind="visitor_milestone",
                    subject=str(target),
                    day=row["day"],
                    timezone_name=timezone_name,
                    value=target,
                )

            history_days = (current_day - first_day).days
            window_days = min(28, history_days)
            if not is_record and window_days >= 14:
                baseline_total = sum(
                    daily_requests.get(
                        (current_day - timedelta(days=offset)).isoformat(), 0
                    )
                    for offset in range(1, window_days + 1)
                )
                baseline = baseline_total / window_days
                if baseline >= 1 and row["requests"] >= max(10, baseline * 3):
                    _insert_summary_event(
                        conn,
                        site_id=site_id,
                        kind="traffic_spike",
                        subject=row["day"],
                        day=row["day"],
                        timezone_name=timezone_name,
                        value=row["requests"],
                    )

    feed_groups: dict[tuple[int, str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT site_id, day, path, reader, reported_subscribers, first_seen
        FROM daily_feed_reader
        WHERE reported_subscribers IS NOT NULL
        ORDER BY site_id, reader, path, day
        """
    ):
        feed_groups[(row["site_id"], row["reader"], row["path"])].append(row)
    for (site_id, reader, path), rows in feed_groups.items():
        previous_high = 0
        for row in rows:
            count = row["reported_subscribers"]
            crossed = [
                target for target in SUBSCRIBER_MILESTONES
                if previous_high < target <= count
            ]
            if crossed:
                target = crossed[-1]
                _insert_summary_event(
                    conn,
                    site_id=site_id,
                    kind="feed_subscriber_milestone",
                    subject=f"{reader}:{path}:{target}",
                    day=row["day"],
                    timezone_name=timezone_name,
                    value=target,
                    path=path,
                    source=reader,
                    occurred_at=row["first_seen"],
                )
            previous_high = max(previous_high, count)
