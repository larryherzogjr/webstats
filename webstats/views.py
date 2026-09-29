"""HTML page routes."""

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, render_template

from .auth import login_required


views_bp = Blueprint("views", __name__)


def _server_today() -> str:
    config = current_app.config["WEBSTATS_CONFIG"]
    return datetime.now(ZoneInfo(config.server.timezone)).date().isoformat()


@views_bp.get("/")
@login_required
def overview():
    return render_template("overview.html", server_today=_server_today())


@views_bp.get("/site/<path:name>")
@login_required
def site(name: str):
    config = current_app.config["WEBSTATS_CONFIG"]
    configured = {site.name for site in config.sites}
    if name not in configured:
        abort(404)
    return render_template(
        "site.html",
        site_name=name,
        geoip_enabled=config.geoip.enabled,
        server_today=_server_today(),
    )


@views_bp.get("/live")
@login_required
def live():
    return render_template("live.html")


@views_bp.get("/ai-crawlers")
@login_required
def ai_crawlers():
    return render_template(
        "ai_crawlers.html", server_today=_server_today(), force_bots=True
    )


@views_bp.get("/health")
@login_required
def health():
    return render_template("health.html")
