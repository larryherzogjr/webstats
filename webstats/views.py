"""HTML page routes."""

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, render_template, request

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


@views_bp.get("/site/<path:name>/page")
@login_required
def page(name: str):
    config = current_app.config["WEBSTATS_CONFIG"]
    configured = {site.name for site in config.sites}
    if name not in configured:
        abort(404)
    page_path = request.args.get("path", "")
    if not page_path or len(page_path) > 2048 or not page_path.startswith("/"):
        abort(400)
    return render_template(
        "page.html",
        site_name=name,
        page_path=page_path,
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
        "ai_crawlers.html", server_today=_server_today(), force_bots=True,
        site_filter=True,
    )


@views_bp.get("/ai-policy")
@login_required
def ai_policy():
    return render_template(
        "ai_policy.html", server_today=_server_today(), force_bots=True,
        hide_assets=True, site_filter=True,
    )


@views_bp.get("/feed-readers")
@login_required
def feed_readers():
    return render_template(
        "feed_readers.html",
        server_today=_server_today(),
        force_bots=True,
        hide_assets=True,
        site_filter=True,
    )


@views_bp.get("/almanac")
@login_required
def almanac():
    return render_template("almanac.html", server_today=_server_today())


@views_bp.get("/briefings")
@login_required
def briefings():
    return render_template("briefings.html", server_today=_server_today())


@views_bp.get("/changes")
@login_required
def changes():
    return render_template(
        "changes.html", server_today=_server_today(), force_bots=True,
        hide_assets=True, site_filter=True,
    )


@views_bp.get("/content")
@login_required
def content_observatory():
    return render_template(
        "content.html", server_today=_server_today(), force_bots=True,
        hide_assets=True, site_filter=True,
    )


@views_bp.get("/episodes")
@login_required
def episodes():
    return render_template(
        "episodes.html", server_today=_server_today(), force_bots=True,
        hide_assets=True, site_filter=True,
    )


@views_bp.get("/reliability")
@login_required
def reliability():
    return render_template(
        "reliability.html", server_today=_server_today(), force_bots=True,
        hide_assets=True, site_filter=True,
    )


@views_bp.get("/pulse")
@login_required
def pulse():
    return render_template("pulse.html", server_today=_server_today())


@views_bp.get("/errors")
@login_required
def errors():
    return render_template("errors.html", server_today=_server_today())


@views_bp.get("/journeys")
@login_required
def journeys():
    return render_template(
        "journeys.html", server_today=_server_today(), force_bots=True,
        hide_assets=True, site_filter=True,
    )


@views_bp.get("/links")
@login_required
def links():
    return render_template(
        "links.html", server_today=_server_today(), force_bots=True,
        hide_assets=True, site_filter=True,
    )


@views_bp.get("/inbox")
@login_required
def inbox():
    return render_template(
        "inbox.html", server_today=_server_today(), force_bots=True,
        hide_assets=True, site_filter=True,
    )


@views_bp.get("/galaxy")
@login_required
def galaxy():
    return render_template(
        "galaxy.html", server_today=_server_today(), force_bots=True,
        hide_assets=True, site_filter=True,
    )


@views_bp.get("/health")
@login_required
def health():
    return render_template("health.html")
