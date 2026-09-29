"""Bounded sitemap discovery for configured public sites."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from gzip import GzipFile
from io import BytesIO
import time
from typing import Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener
from xml.etree import ElementTree
import zlib

from .parser import ASSET_EXTENSIONS
from .robots import _SameSiteRedirect


_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_SECONDS = 30 * 60
_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_MAX_DOCUMENTS = 24
_MAX_URLS = 10_000
_MAX_DISCOVERY_SECONDS = 8


def _canonical_host(value: str) -> str:
    return value.lower().rstrip(".").removeprefix("www.")


def _same_site(url: str, site: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.port in {None, 443}
        and _canonical_host(parsed.hostname or "") == _canonical_host(site)
    )


def sitemap_declarations(robots_text: str, site: str) -> list[str]:
    """Return safe same-site Sitemap declarations from robots.txt."""
    urls = []
    for raw_line in robots_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        if field.lower() == "sitemap" and _same_site(value, site):
            urls.append(value)
    return list(dict.fromkeys(urls))


def _download_bytes(url: str, site: str, timeout: float) -> dict:
    if not _same_site(url, site):
        return {"url": url, "status": "rejected", "body": b""}
    request = Request(url, headers={"User-Agent": "Webstats-Content-Observer/1.0"})
    try:
        with build_opener(_SameSiteRedirect()).open(request, timeout=timeout) as response:
            body = response.read(_MAX_DOCUMENT_BYTES + 1)
            if len(body) > _MAX_DOCUMENT_BYTES:
                return {"url": url, "status": "too_large", "body": b""}
            if response.headers.get("Content-Encoding", "").lower() == "gzip" or response.geturl().lower().endswith(".gz"):
                with GzipFile(fileobj=BytesIO(body)) as compressed:
                    body = compressed.read(_MAX_DOCUMENT_BYTES + 1)
                if len(body) > _MAX_DOCUMENT_BYTES:
                    return {"url": url, "status": "too_large", "body": b""}
            return {
                "url": response.geturl(), "status": "available", "body": body,
            }
    except HTTPError as error:
        return {
            "url": url,
            "status": "not_found" if error.code == 404 else "http_error",
            "http_status": error.code,
            "body": b"",
        }
    except (OSError, URLError, TimeoutError, EOFError, zlib.error) as error:
        return {
            "url": url, "status": "unavailable",
            "detail": type(error).__name__, "body": b"",
        }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(body: bytes, source_url: str) -> dict:
    """Parse one sitemap document without treating image/video locs as pages."""
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, ValueError) as error:
        return {
            "kind": "invalid", "urls": [], "sitemaps": [],
            "error": type(error).__name__,
        }
    kind = _local_name(root.tag)
    if kind == "sitemapindex":
        nested = []
        for child in root:
            if _local_name(child.tag) != "sitemap":
                continue
            loc = next(
                (item.text.strip() for item in child if _local_name(item.tag) == "loc" and item.text),
                "",
            )
            if loc:
                nested.append(urljoin(source_url, loc))
        return {"kind": kind, "urls": [], "sitemaps": nested}
    if kind != "urlset":
        return {"kind": "invalid", "urls": [], "sitemaps": [], "error": "unknown_root"}
    urls = []
    for child in root:
        if _local_name(child.tag) != "url":
            continue
        values = {
            _local_name(item.tag): item.text.strip()
            for item in child if item.text and _local_name(item.tag) in {"loc", "lastmod"}
        }
        if values.get("loc"):
            urls.append({
                "url": urljoin(source_url, values["loc"]),
                "lastmod": values.get("lastmod"),
            })
    return {"kind": kind, "urls": urls, "sitemaps": []}


def _discover_site(site: str, robots: dict, timeout: float) -> dict:
    declared = sitemap_declarations(robots.get("text", ""), site)
    seeds = declared or [
        f"https://{site}/sitemap.xml",
        f"https://{site}/sitemap_index.xml",
        f"https://{site}/sitemap-index.xml",
    ]
    queue = list(seeds)
    visited: set[str] = set()
    successful_documents = []
    errors = []
    pages: dict[str, dict] = {}
    deadline = time.monotonic() + _MAX_DISCOVERY_SECONDS
    while (
        queue and len(visited) < _MAX_DOCUMENTS and len(pages) < _MAX_URLS
        and time.monotonic() < deadline
    ):
        url = queue.pop(0)
        if url in visited or not _same_site(url, site):
            continue
        visited.add(url)
        remaining = max(0.25, deadline - time.monotonic())
        downloaded = _download_bytes(url, site, min(timeout, remaining))
        if downloaded["status"] != "available":
            errors.append({
                "url": url, "status": downloaded["status"],
                **({"http_status": downloaded["http_status"]} if downloaded.get("http_status") else {}),
            })
            continue
        parsed = parse_sitemap(downloaded["body"], downloaded["url"])
        if parsed["kind"] == "invalid":
            errors.append({"url": url, "status": "invalid"})
            continue
        successful_documents.append(downloaded["url"])
        for nested in parsed["sitemaps"]:
            if _same_site(nested, site) and nested not in visited:
                queue.append(nested)
        for item in parsed["urls"]:
            if not _same_site(item["url"], site):
                continue
            path = urlsplit(item["url"]).path or "/"
            if path.lower().endswith(ASSET_EXTENSIONS):
                continue
            existing = pages.get(path)
            if existing is None or (item.get("lastmod") or "") > (existing.get("lastmod") or ""):
                pages[path] = {
                    "path": path, "url": item["url"],
                    "lastmod": item.get("lastmod"), "sitemap": downloaded["url"],
                }
            if len(pages) >= _MAX_URLS:
                break
    if successful_documents:
        status = "available"
    elif errors and all(item["status"] == "not_found" for item in errors):
        status = "not_found"
    else:
        status = "unavailable"
    return {
        "site": site,
        "status": status,
        "declared": declared,
        "documents": successful_documents,
        "pages": sorted(pages.values(), key=lambda item: item["path"]),
        "errors": errors,
        "limited": bool(queue) or len(pages) >= _MAX_URLS,
    }


def fetch_sitemap_inventories(
    sites: Iterable[str], robots: dict[str, dict], timeout: float = 2.5,
    now: Optional[float] = None,
) -> dict[str, dict]:
    """Discover configured sites concurrently and cache current inventories."""
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
            discovered = executor.map(
                lambda name: _discover_site(name, robots[name], timeout), missing
            )
            for item in discovered:
                _CACHE[item["site"]] = (timestamp, item)
                results[item["site"]] = item
    return {site: results[site] for site in names}
