"""Конфигурация из переменных окружения (+ необязательный файл .env)."""
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #")[0].strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)


def load_config(overrides=None):
    _load_dotenv(ROOT / ".env")
    e = os.environ.get
    env = e("APP_ENV", "development")
    cfg = dict(
        ENV=env,
        SECRET_KEY=e("SECRET_KEY") or None,
        DATABASE_PATH=e("DATABASE_PATH", str(ROOT / "instance" / "teacherai.db")),
        LOG_DIR=e("LOG_DIR", str(ROOT / "logs")),
        BACKUP_DIR=e("BACKUP_DIR", str(ROOT / "backups")),
        PUBLIC_URL=e("PUBLIC_URL", "").rstrip("/"),
        AI_PROVIDER=e("AI_PROVIDER", "anthropic").lower(),
        AI_API_KEY=e("AI_API_KEY", ""),
        AI_MODEL=e("AI_MODEL", ""),
        AI_BASE_URL=e("AI_BASE_URL", ""),
        AI_TIMEOUT=int(e("AI_TIMEOUT", "180")),
        AI_MAX_TOKENS=int(e("AI_MAX_TOKENS", "8000")),
        AI_PRICE_INPUT=float(e("AI_PRICE_INPUT_PER_MTOK", "0") or 0),
        AI_PRICE_OUTPUT=float(e("AI_PRICE_OUTPUT_PER_MTOK", "0") or 0),
        DAILY_LIMIT=int(e("DAILY_GENERATION_LIMIT", "30")),
        MAX_TASKS_TOTAL=120,
        TZ_OFFSET_HOURS=int(e("TZ_OFFSET_HOURS", "3")),
        SMTP_HOST=e("SMTP_HOST", ""),
        SMTP_PORT=int(e("SMTP_PORT", "587") or 587),
        SMTP_USER=e("SMTP_USER", ""),
        SMTP_PASSWORD=e("SMTP_PASSWORD", ""),
        SMTP_FROM=e("SMTP_FROM", ""),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=(env == "production"),
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,
        TESTING=False,
        RATE_LIMITS=True,
    )
    if overrides:
        cfg.update(overrides)
    if not cfg["SECRET_KEY"]:
        if cfg["ENV"] == "production":
            raise RuntimeError("В production обязательно задайте SECRET_KEY в окружении.")
        cfg["SECRET_KEY"] = secrets.token_hex(32)
    return cfg
