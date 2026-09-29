"""Persistent, bounded observation of how configured public sites evolve."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .config import Config, load_config
from .db import connect, initialize, site_id_map
from .robots import fetch_robots_policies
from .sitemaps import fetch_sitemap_inventories


MAX_REMOVAL_PROBES = 64


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _page_digest(pages: list[dict]) -> str:
    stable = [(item["path"], item.get("lastmod") or "") for item in pages]
    return _digest(json.dumps(stable, separators=(",", ":"), ensure_ascii=True))


def _same_site(url: str, site: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    expected = site.lower().removeprefix("www.")
    return parsed.scheme == "https" and parsed.port in {None, 443} and host == expected


def probe_page(url: str, site: str, timeout: float = 3.0) -> dict:
    """Classify a removed sitemap URL without following off-site redirects."""
    if not _same_site(url, site):
        return {"status": None, "redirect_to": None}
    request = Request(
        url,
        headers={
            "User-Agent": "Webstats-Chronicle/1.0",
            "Range": "bytes=0-0",
        },
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            return {"status": response.status, "redirect_to": None}
    except HTTPError as error:
        location = error.headers.get("Location") if error.headers else None
        redirect = urljoin(url, location) if location else None
        if redirect and not _same_site(redirect, site):
            redirect = None
        return {"status": error.code, "redirect_to": redirect}
    except (OSError, URLError, TimeoutError):
        return {"status": None, "redirect_to": None}


def _event(
    conn, *, site_id: int, snapshot_id: int, captured_at: int, day: str, kind: str,
    summary: str, path: Optional[str] = None, from_value: Optional[str] = None,
    to_value: Optional[str] = None,
) -> None:
    identity = "\0".join(
        [str(site_id), str(captured_at), kind, path or "", from_value or "", to_value or ""]
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO chronicle_events(
            site_id, snapshot_id, occurred_at, day, kind, path, summary,
            from_value, to_value, event_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            site_id, snapshot_id, captured_at,
            day,
            kind, path, summary, from_value, to_value, _digest(identity),
        ),
    )


def scan(
    config: Config, *, now: Optional[int] = None,
    robots_fetcher: Callable = fetch_robots_policies,
    sitemap_fetcher: Callable = fetch_sitemap_inventories,
    page_probe: Callable = probe_page,
) -> dict:
    """Fetch current public policy/inventory and persist meaningful changes."""
    captured_at = int(time.time()) if now is None else int(now)
    scan_day = datetime.fromtimestamp(
        captured_at, ZoneInfo(config.server.timezone)
    ).date().isoformat()
    names = [site.name for site in config.sites]
    robots = robots_fetcher(names)
    inventories = sitemap_fetcher(names, robots)
    totals = {"sites": len(names), "snapshots": 0, "events": 0, "probes": 0}

    with connect(config.storage.db_path) as conn:
        initialize(conn, config)
        ids = site_id_map(conn)
        for name in names:
            site_id = ids[name]
            policy = robots[name]
            inventory = inventories[name]
            pages = inventory.get("pages", [])
            robots_text = policy.get("text", "") if policy.get("status") == "available" else ""
            robots_digest = _digest(robots_text)
            sitemap_digest = _page_digest(pages)
            previous = conn.execute(
                "SELECT * FROM chronicle_snapshots WHERE site_id=? "
                "ORDER BY captured_at DESC, id DESC LIMIT 1",
                (site_id,),
            ).fetchone()
            previous_policy = conn.execute(
                "SELECT * FROM chronicle_snapshots "
                "WHERE site_id=? AND robots_status='available' "
                "ORDER BY captured_at DESC, id DESC LIMIT 1",
                (site_id,),
            ).fetchone()
            policy_archived = conn.execute(
                """
                SELECT 1 FROM chronicle_snapshots
                WHERE site_id=? AND robots_status=? AND robots_digest=? LIMIT 1
                """,
                (site_id, policy.get("status", "unavailable"), robots_digest),
            ).fetchone()
            archived_robots_text = "" if policy_archived else robots_text
            known = {
                row["path"]: row for row in conn.execute(
                    "SELECT * FROM chronicle_pages WHERE site_id=?", (site_id,)
                )
            }
            current_paths = {item["path"] for item in pages}
            inventory_complete = (
                inventory.get("status") == "available"
                and not inventory.get("errors")
                and not inventory.get("limited")
            )
            probe_results = []
            if previous is not None and inventory_complete:
                newly_removed = [
                    row for path, row in known.items()
                    if row["state"] == "published" and path not in current_paths
                ]
                monitored = [
                    row for path, row in known.items()
                    if row["state"] != "published" and path not in current_paths
                ]
                for old in (newly_removed + monitored)[:MAX_REMOVAL_PROBES]:
                    # Network I/O happens before this site's first write, so a
                    # slow origin never holds SQLite's writer lock.
                    probe_results.append((old, page_probe(old["url"], name)))
                    totals["probes"] += 1

            before_events = conn.total_changes
            cursor = conn.execute(
                """
                INSERT INTO chronicle_snapshots(
                    site_id, captured_at, day, sitemap_status, sitemap_digest,
                    page_count, robots_status, robots_digest, robots_text,
                    error_count, limited
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    site_id, captured_at,
                    scan_day,
                    inventory.get("status", "unavailable"), sitemap_digest, len(pages),
                    policy.get("status", "unavailable"), robots_digest,
                    archived_robots_text,
                    len(inventory.get("errors", [])), int(bool(inventory.get("limited"))),
                ),
            )
            snapshot_id = cursor.lastrowid
            totals["snapshots"] += 1

            if previous is None:
                _event(
                    conn, site_id=site_id, snapshot_id=snapshot_id,
                    captured_at=captured_at, day=scan_day, kind="baseline",
                    summary=f"Established a baseline with {len(pages):,} published pages.",
                    to_value=str(len(pages)),
                )
            else:
                if (
                    policy.get("status") == "available"
                    and previous_policy is not None
                    and previous_policy["robots_digest"] != robots_digest
                ):
                    _event(
                        conn, site_id=site_id, snapshot_id=snapshot_id,
                        captured_at=captured_at, day=scan_day, kind="robots_changed",
                        summary="robots.txt policy changed.",
                        from_value=previous_policy["robots_digest"],
                        to_value=robots_digest,
                    )
                if previous["sitemap_status"] != inventory.get("status"):
                    _event(
                        conn, site_id=site_id, snapshot_id=snapshot_id,
                        captured_at=captured_at, day=scan_day, kind="sitemap_status_changed",
                        summary=(
                            f"Sitemap discovery changed from {previous['sitemap_status']} "
                            f"to {inventory.get('status', 'unavailable')}."
                        ),
                        from_value=previous["sitemap_status"],
                        to_value=inventory.get("status", "unavailable"),
                    )

            baseline = previous is None
            for item in pages:
                old = known.get(item["path"])
                if old is None:
                    conn.execute(
                        """
                        INSERT INTO chronicle_pages(
                            site_id, path, url, lastmod, first_seen_at, last_seen_at,
                            state
                        ) VALUES (?, ?, ?, ?, ?, ?, 'published')
                        """,
                        (site_id, item["path"], item["url"], item.get("lastmod"), captured_at, captured_at),
                    )
                    if not baseline:
                        _event(
                            conn, site_id=site_id, snapshot_id=snapshot_id,
                            captured_at=captured_at, day=scan_day, kind="published", path=item["path"],
                            summary="A page appeared in the public sitemap.",
                            to_value=item.get("lastmod"),
                        )
                else:
                    previous_lastmod = old["lastmod"]
                    conn.execute(
                        """
                        UPDATE chronicle_pages SET url=?, lastmod=?, last_seen_at=?,
                            removed_at=NULL, state='published', http_status=NULL,
                            redirect_to=NULL WHERE site_id=? AND path=?
                        """,
                        (item["url"], item.get("lastmod"), captured_at, site_id, item["path"]),
                    )
                    if old["state"] != "published":
                        _event(
                            conn, site_id=site_id, snapshot_id=snapshot_id,
                            captured_at=captured_at, day=scan_day, kind="republished", path=item["path"],
                            summary="A previously removed page returned to the sitemap.",
                            from_value=old["state"], to_value=item.get("lastmod"),
                        )
                    elif item.get("lastmod") and previous_lastmod and item["lastmod"] != previous_lastmod:
                        _event(
                            conn, site_id=site_id, snapshot_id=snapshot_id,
                            captured_at=captured_at, day=scan_day, kind="updated", path=item["path"],
                            summary="The sitemap last-modified value changed.",
                            from_value=previous_lastmod, to_value=item["lastmod"],
                        )

            # Partial, failed, or capped inventories are observations, never
            # evidence that missing pages were removed.
            if previous is not None and inventory_complete:
                for old, result in probe_results:
                    status = result.get("status")
                    redirect_to = result.get("redirect_to")
                    if status is None:
                        continue
                    if status in {301, 302, 303, 307, 308} and redirect_to:
                        kind, state = "redirected", "redirected"
                        target_path = urlsplit(redirect_to).path or "/"
                        summary = f"The removed page now redirects to {target_path}."
                    elif status in {404, 410}:
                        kind, state = "disappeared", "disappeared"
                        summary = f"The removed page now returns HTTP {status}."
                    else:
                        kind, state = "sitemap_removed", "unlisted"
                        summary = (
                            "The page is reachable again but remains outside the sitemap."
                            if old["state"] in {"redirected", "disappeared"}
                            else "The page left the sitemap but remains reachable."
                        )
                    changed = (
                        old["state"] != state
                        or old["redirect_to"] != redirect_to
                        or (
                            state == "disappeared"
                            and old["http_status"] != status
                        )
                    )
                    conn.execute(
                        """
                        UPDATE chronicle_pages SET removed_at=COALESCE(removed_at, ?),
                            state=?, http_status=?,
                            redirect_to=? WHERE site_id=? AND path=?
                        """,
                        (captured_at, state, status, redirect_to, site_id, old["path"]),
                    )
                    if old["state"] == "published" or changed:
                        _event(
                            conn, site_id=site_id, snapshot_id=snapshot_id,
                            captured_at=captured_at, day=scan_day, kind=kind, path=old["path"],
                            summary=summary, from_value=old["state"],
                            to_value=redirect_to or (str(status) if status else state),
                        )
            totals["events"] += max(0, conn.total_changes - before_events - 1)
            conn.commit()

    # Count actual event rows rather than page-state writes.
    with connect(config.storage.db_path, readonly=True) as conn:
        totals["events"] = conn.execute(
            "SELECT COUNT(*) FROM chronicle_events WHERE occurred_at=?", (captured_at,)
        ).fetchone()[0]
    return totals


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/webstats/config.toml")
    args = parser.parse_args(argv)
    try:
        result = scan(load_config(Path(args.config)))
    except Exception as exc:
        print(f"Chronicle scan failed: {exc}")
        return 1
    print(
        f"Chronicle: {result['sites']} sites, {result['snapshots']} snapshots, "
        f"{result['events']} events, {result['probes']} removal probes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
