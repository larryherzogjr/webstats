"""Flask application factory and development entry point."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Optional

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from .api import api_bp
from .auth import auth_bp
from .config import Config, load_config
from .db import connect, initialize
from .views import views_bp


def create_app(config: Config | None = None, config_path: str | Path | None = None) -> Flask:
    if config is None:
        path = config_path or os.environ.get("WEBSTATS_CONFIG", "/etc/webstats/config.toml")
        config = load_config(path)
    app = Flask(__name__)
    # The production service listens only on loopback behind the bundled nginx
    # configuration, so exactly one forwarded hop is trusted.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    app.config.update(
        SECRET_KEY=config.server.secret_key,
        SESSION_COOKIE_SECURE=os.environ.get("WEBSTATS_INSECURE_COOKIE") != "1",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
        WEBSTATS_CONFIG=config,
    )
    with connect(config.storage.db_path) as conn:
        initialize(conn, config)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(views_bp)
    return app


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/webstats/config.toml")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    app = create_app(config_path=args.config)
    host, port = app.config["WEBSTATS_CONFIG"].server.bind.rsplit(":", 1)
    app.run(host=host, port=int(port), debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
