#!/usr/bin/env python3
"""Команды обслуживания: init-db, create-admin, reset-password, backup."""
import argparse
import getpass
import os
import shutil
import sqlite3
import sys
from datetime import datetime

from werkzeug.security import generate_password_hash

from app import db as database
from app.auth import check_password_strength
from app.config import load_config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["init-db", "create-admin", "reset-password", "backup"])
    ap.add_argument("--email")
    a = ap.parse_args()
    cfg = load_config()
    path = cfg["DATABASE_PATH"]
    if a.cmd == "init-db":
        done = database.migrate(path)
        print("Применены миграции:", ", ".join(done) if done else "новых нет")
    elif a.cmd == "create-admin":
        database.migrate(path)
        email = (a.email or os.environ.get("ADMIN_EMAIL") or input("Email администратора: ")).strip().lower()
        pw = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Пароль (не короче 8, буквы и цифры): ")
        if (m := check_password_strength(pw)):
            sys.exit(m)
        name = os.environ.get("ADMIN_NAME", "Администратор")
        con = database.connect(path)
        u = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if u:
            con.execute("UPDATE users SET role='admin', status='active', password_hash=? WHERE id=?",
                        (generate_password_hash(pw), u["id"]))
        else:
            con.execute("INSERT INTO users (name,email,password_hash,role,status,created_at) VALUES (?,?,?,'admin','active',?)",
                        (name, email, generate_password_hash(pw), database.utcnow()))
        con.commit()
        print(f"Администратор {email} готов.")
    elif a.cmd == "reset-password":
        email = (a.email or input("Email пользователя: ")).strip().lower()
        pw = getpass.getpass("Новый пароль: ")
        if (m := check_password_strength(pw)):
            sys.exit(m)
        con = database.connect(path)
        n = con.execute("UPDATE users SET password_hash=? WHERE email=?", (generate_password_hash(pw), email)).rowcount
        con.commit()
        print("Пароль изменён." if n else "Пользователь не найден.")
    elif a.cmd == "backup":
        os.makedirs(cfg["BACKUP_DIR"], exist_ok=True)
        dest = os.path.join(cfg["BACKUP_DIR"], f"teacherai-{datetime.now():%Y%m%d-%H%M%S}.db")
        src, dst = sqlite3.connect(path), sqlite3.connect(dest)
        src.backup(dst)  # безопасная копия работающей БД
        dst.close(); src.close()
        backups = sorted(f for f in os.listdir(cfg["BACKUP_DIR"]) if f.endswith(".db"))
        for old in backups[:-14]:
            os.remove(os.path.join(cfg["BACKUP_DIR"], old))
        print("Резервная копия:", dest)


if __name__ == "__main__":
    main()
