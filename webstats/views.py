"""HTML page routes."""

from flask import Blueprint, abort, current_app, render_template

from .auth import login_required


views_bp = Blueprint("views", __name__)


@views_bp.get("/")
@login_required
def overview():
    return render_template("overview.html")


@views_bp.get("/site/<path:name>")
@login_required
def site(name: str):
    config = current_app.config["WEBSTATS_CONFIG"]
    configured = {site.name for site in config.sites}
    if name not in configured:
        abort(404)
    return render_template(
        "site.html", site_name=name, geoip_enabled=config.geoip.enabled
    )


@views_bp.get("/live")
@login_required
def live():
    return render_template("live.html")


@views_bp.get("/health")
@login_required
def health():
    return render_template("health.html")
