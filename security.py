"""CSRF, декораторы доступа, ограничение частоты запросов."""
import hmac
import secrets
import time
from collections import defaultdict, deque
from functools import wraps

from flask import abort, current_app, g, redirect, request, session, url_for


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.headers.get("X-CSRFToken") or request.form.get("csrf_token", "")
    real = session.get("csrf", "")
    if not real or not hmac.compare_digest(str(sent), str(real)):
        abort(400, "Сессия формы устарела. Обновите страницу и повторите действие.")


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        if g.user["role"] != "admin":  # роль читается из БД на каждом запросе
            abort(403)
        return f(*a, **kw)
    return wrapper


class RateLimiter:
    """Скользящее окно в памяти процесса. Для нескольких воркеров используйте Redis/nginx limit_req."""

    def __init__(self):
        self.hits = defaultdict(deque)

    def allow(self, key, limit, window):
        if not current_app.config.get("RATE_LIMITS", True):
            return True
        now = time.time()
        q = self.hits[key]
        while q and q[0] < now - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def reset(self):
        self.hits.clear()


limiter = RateLimiter()


def rate_limit(name, limit, window, per_user=False, methods=("POST",)):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if request.method not in methods:
                return f(*a, **kw)
            ident = g.user["id"] if (per_user and g.user) else request.remote_addr
            if not limiter.allow(f"{name}:{ident}", limit, window):
                abort(429)
            return f(*a, **kw)
        return wrapper
    return deco
