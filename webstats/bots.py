"""Conservative user-agent classification."""

from __future__ import annotations

import re
from typing import Tuple


KNOWN_BOTS = (
    ("Googlebot", "Googlebot"),
    ("bingbot", "Bingbot"),
    ("DuckDuckBot", "DuckDuckBot"),
    ("Applebot", "Applebot"),
    ("AhrefsBot", "AhrefsBot"),
    ("SemrushBot", "SemrushBot"),
    ("PetalBot", "PetalBot"),
    ("Bytespider", "Bytespider"),
    ("GPTBot", "GPTBot"),
    ("ChatGPT-User", "ChatGPT-User"),
    ("OAI-SearchBot", "OAI-SearchBot"),
    ("ClaudeBot", "ClaudeBot"),
    ("Claude-User", "Claude-User"),
    ("Claude-SearchBot", "Claude-SearchBot"),
    ("PerplexityBot", "PerplexityBot"),
    ("Perplexity-User", "Perplexity-User"),
    ("CCBot", "CCBot"),
    ("facebookexternalhit", "Facebook"),
    ("Twitterbot", "Twitterbot"),
    ("LinkedInBot", "LinkedInBot"),
    ("UptimeRobot", "UptimeRobot"),
)

AI_AGENTS = {
    "GPTBot": ("OpenAI", "Model training"),
    "ChatGPT-User": ("OpenAI", "User-requested retrieval"),
    "OAI-SearchBot": ("OpenAI", "Search"),
    "ClaudeBot": ("Anthropic", "Model training"),
    "Claude-User": ("Anthropic", "User-requested retrieval"),
    "Claude-SearchBot": ("Anthropic", "Search"),
    "PerplexityBot": ("Perplexity", "Search"),
    "Perplexity-User": ("Perplexity", "User-requested retrieval"),
    "Bytespider": ("ByteDance", "AI crawler"),
    "CCBot": ("Common Crawl", "Open web corpus"),
}

GENERIC_BOTS = (
    ("python-requests", "python-requests"),
    ("curl/", "curl"),
    ("wget/", "wget"),
    ("go-http-client", "Go HTTP client"),
    ("scrapy", "Scrapy"),
    ("headlesschrome", "HeadlessChrome"),
)


def classify_user_agent(user_agent: str) -> Tuple[str, bool]:
    ua = (user_agent or "").strip()
    lowered = ua.lower()
    if not ua or ua == "-":
        return "Empty user agent", True
    for needle, family in KNOWN_BOTS:
        if needle.lower() in lowered:
            return family, True
    for needle, family in GENERIC_BOTS:
        if needle in lowered:
            return family, True
    if "bot" in lowered or "crawler" in lowered or "spider" in lowered:
        return "Other bot", True
    if not ua.startswith("Mozilla/"):
        return "Non-browser client", True
    return browser_family(ua), False


def browser_family(user_agent: str) -> str:
    checks = (
        (r"Edg/", "Edge"),
        (r"OPR/|Opera/", "Opera"),
        (r"CriOS/|Chrome/", "Chrome"),
        (r"FxiOS/|Firefox/", "Firefox"),
        (r"Version/.+Safari/", "Safari"),
    )
    for pattern, name in checks:
        if re.search(pattern, user_agent):
            return name
    return "Other browser"


def os_family(user_agent: str) -> str:
    checks = (
        ("Windows", "Windows"),
        ("Android", "Android"),
        ("iPhone", "iOS"),
        ("iPad", "iPadOS"),
        ("Mac OS X", "macOS"),
        ("Linux", "Linux"),
        ("CrOS", "ChromeOS"),
    )
    for needle, name in checks:
        if needle in user_agent:
            return name
    return "Other"
