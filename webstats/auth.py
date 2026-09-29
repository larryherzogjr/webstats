"""Single-admin session authentication and login throttling."""

from __future__ import annotations

from collections import defaultdict, deque
from functools import wraps
import hashlib
import hmac
import time
from typing import Callable, Deque, TypeVar

import bcrypt
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from .config import Config


auth_bp = Blueprint("auth", __name__)
_attempts: dict[str, Deque[float]] = defaultdict(deque)
_WINDOW_SECONDS = 15 * 60
_MAX_ATTEMPTS = 8
F = TypeVar("F", bound=Callable)


def _credential_token(config: Config) -> str:
    value = f"{config.server.admin_user}\0{config.server.admin_password_hash}"
    return hashlib.sha256(value.encode()).hexdigest()


def login_required(view: F) -> F:
    @wraps(view)
    def wrapped(*args, **kwargs):
        config = current_app.config["WEBSTATS_CONFIG"]
        token = session.get("credential_token", "")
        authenticated = (
            session.permanent
            and session.get("authenticated")
            and hmac.compare_digest(token, _credential_token(config))
        )
        if not authenticated:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped  # type: ignore[return-value]


def _client_key() -> str:
    return request.remote_addr or "unknown"


def _is_limited(key: str) -> bool:
    now = time.monotonic()
    attempts = _attempts[key]
    while attempts and attempts[0] < now - _WINDOW_SECONDS:
        attempts.popleft()
    return len(attempts) >= _MAX_ATTEMPTS


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        key = _client_key()
        if _is_limited(key):
            error = "Too many login attempts. Try again later."
            return render_template("login.html", error=error), 429
        config = current_app.config["WEBSTATS_CONFIG"]
        username = request.form.get("username", "")
        password = request.form.get("password", "").encode()
        stored = config.server.admin_password_hash.encode()
        try:
            valid = username == config.server.admin_user and bcrypt.checkpw(password, stored)
        except ValueError:
            valid = False
        if valid:
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            session["credential_token"] = _credential_token(config)
            _attempts.pop(key, None)
            target = request.args.get("next", "")
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("views.overview")
            return redirect(target)
        _attempts[key].append(time.monotonic())
        error = "Incorrect username or password."
    return render_template("login.html", error=error)


@auth_bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
