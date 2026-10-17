import json
import logging
import os
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from flask import Flask, g, render_template, request, session

from config import load_config
from security import check_csrf, csrf_token


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.update(load_config(overrides))
    app.secret_key = app.config["SECRET_KEY"]

    from app import ai, db
    
    app.extensions["ai_provider"] = ai.build_provider(app.config)
    _setup_logging(app)
    db.migrate(app.config["DATABASE_PATH"])
    app.teardown_appcontext(db.close_db)

    from . import admin, auth, public, teacher
    for bp in (auth.bp, teacher.bp, admin.bp, public.bp):
        app.register_blueprint(bp)

    @app.before_request
    def load_user():
        g.user = None
        uid = session.get("uid")
        if uid:
            u = db.query("SELECT * FROM users WHERE id=?", (uid,), one=True)
            if u and u["status"] == "active":
                g.user = u
                last = u["last_active_at"] or ""
                if last < (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"):
                    db.execute("UPDATE users SET last_active_at=? WHERE id=?", (db.utcnow(), uid))
            else:
                session.clear()
        check_csrf()

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'self'")
        if request.path.startswith(("/admin", "/materials", "/history", "/dashboard")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def inject():
        return {"csrf_token": csrf_token, "ai_ready": app.extensions.get("ai_provider") is not None,
                "ai_model": app.config["AI_MODEL"]}

    off = app.config["TZ_OFFSET_HOURS"]

    def _parse(s):
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") + timedelta(hours=off)

    app.jinja_env.filters["dt"] = lambda s: _parse(s).strftime("%d.%m.%Y %H:%M") if s else "—"
    app.jinja_env.filters["d"] = lambda s: _parse(s).strftime("%d.%m.%Y") if s else "—"
    app.jinja_env.filters["fromjson"] = lambda s: json.loads(s) if s else {}
    app.jinja_env.filters["pretty"] = lambda v: json.dumps(v, ensure_ascii=False, indent=2)
    app.jinja_env.filters["num"] = lambda x: (str(int(x)) if float(x).is_integer() else str(x).replace(".", ",")) if x is not None else "—"

    for code, title, text in ((400, "Некорректный запрос", None), (403, "Нет доступа", "У вас нет прав для просмотра этой страницы."),
                              (404, "Страница не найдена", "Такой страницы нет или она была удалена."),
                              (413, "Слишком большой запрос", "Уменьшите объём данных."),
                              (429, "Слишком много запросов", "Подождите немного и повторите попытку."),
                              (500, "Ошибка сервера", "Мы уже записали ошибку. Попробуйте позже.")):
        def handler(e, code=code, title=title, text=text):
            msg = text or getattr(e, "description", "") or ""
            return render_template("error.html", code=code, title=title, text=msg), code
        app.register_error_handler(code, handler)
    return app


def _setup_logging(app):
    try:
        os.makedirs(app.config["LOG_DIR"], exist_ok=True)
        h = RotatingFileHandler(os.path.join(app.config["LOG_DIR"], "app.log"), maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        h.setLevel(logging.WARNING if app.config["TESTING"] else logging.INFO)
        app.logger.addHandler(h)
        app.logger.setLevel(logging.INFO)
    except OSError:
        pass
