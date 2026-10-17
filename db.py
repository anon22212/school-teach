"""Работа с БД (SQLite, WAL) и применение SQL-миграций из каталога migrations/."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.create_function("rulower", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE_PATH"])
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def migrate(path):
    """Применяет непримёненные миграции. Возвращает список применённых."""
    con = connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    done = {r[0] for r in con.execute("SELECT name FROM schema_migrations")}
    applied = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if f.name in done:
            continue
        con.executescript(f.read_text(encoding="utf-8"))
        con.execute("INSERT INTO schema_migrations VALUES (?,?)", (f.name, utcnow()))
        con.commit()
        applied.append(f.name)
    con.close()
    return applied
