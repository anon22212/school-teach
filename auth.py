import hashlib
import re
import secrets
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db
from .security import login_required, rate_limit

bp = Blueprint("auth", __name__)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
DUMMY_HASH = generate_password_hash("dummy-password-for-timing")


def check_password_strength(pw):
    if len(pw) < 8:
        return "Пароль должен быть не короче 8 символов."
    if not re.search(r"[A-Za-zА-Яа-яЁё]", pw) or not re.search(r"\d", pw):
        return "Пароль должен содержать буквы и цифры."
    return None


def _start_session(user_id):
    session.clear()  # защита от фиксации сессии
    session["uid"] = user_id
    session.permanent = True
    db.execute("UPDATE users SET last_login_at=?, last_active_at=? WHERE id=?", (db.utcnow(), db.utcnow(), user_id))


def _safe_next(url):
    return url if url and url.startswith("/") and not url.startswith("//") else None


@bp.route("/register", methods=["GET", "POST"])
@rate_limit("register", 10, 3600)
def register():
    if g.user:
        return redirect(url_for("teacher.dashboard"))
    errors, form = {}, request.form
    if request.method == "POST":
        name = " ".join(form.get("name", "").split())[:80]
        email = form.get("email", "").strip().lower()[:120]
        pw = form.get("password", "")
        if len(name) < 2:
            errors["name"] = "Укажите имя (от 2 символов)."
        if not EMAIL_RE.match(email):
            errors["email"] = "Введите корректный email."
        if (m := check_password_strength(pw)):
            errors["password"] = m
        if pw != form.get("password2", ""):
            errors["password2"] = "Пароли не совпадают."
        if form.get("agree") != "1":
            errors["agree"] = "Необходимо согласие с условиями."
        if not errors and db.query("SELECT 1 FROM users WHERE email=?", (email,), one=True):
            errors["email"] = "Этот email уже зарегистрирован."
        if not errors:
            uid = db.execute("INSERT INTO users (name,email,password_hash,role,status,created_at) VALUES (?,?,?,'teacher','active',?)",
                             (name, email, generate_password_hash(pw), db.utcnow()))
            _start_session(uid)
            return redirect(url_for("teacher.dashboard"))
    return render_template("auth/register.html", errors=errors, form=form)


@bp.route("/login", methods=["GET", "POST"])
@rate_limit("login", 10, 300)
def login():
    if g.user:
        return redirect(url_for("teacher.dashboard"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        u = db.query("SELECT * FROM users WHERE email=?", (email,), one=True)
        ok = check_password_hash(u["password_hash"] if u else DUMMY_HASH, pw)
        if u and ok and u["status"] == "active":
            _start_session(u["id"])
            nxt = _safe_next(request.args.get("next"))
            return redirect(nxt or (url_for("admin.dashboard") if u["role"] == "admin" else url_for("teacher.dashboard")))
        error = "Доступ к аккаунту приостановлен. Обратитесь к администратору." if (u and ok) else "Неверный email или пароль."
    return render_template("auth/login.html", error=error)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("public.index"))


def send_mail(to, subject, body):
    c = current_app.config
    if not (c["SMTP_HOST"] and c["SMTP_FROM"]):
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = c["SMTP_FROM"], to, subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(c["SMTP_HOST"], c["SMTP_PORT"], timeout=15) as s:
            s.starttls()
            if c["SMTP_USER"]:
                s.login(c["SMTP_USER"], c["SMTP_PASSWORD"])
            s.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException):
        current_app.logger.warning("Не удалось отправить письмо восстановления")
        return False


@bp.route("/forgot", methods=["GET", "POST"])
@rate_limit("forgot", 5, 3600)
def forgot():
    sent = mail_ok = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        mail_ok = bool(current_app.config["SMTP_HOST"] and current_app.config["SMTP_FROM"])
        u = db.query("SELECT * FROM users WHERE email=? AND status='active'", (email,), one=True)
        if u and mail_ok:
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO password_resets (user_id,token_hash,expires_at) VALUES (?,?,?)",
                       (u["id"], hashlib.sha256(token.encode()).hexdigest(),
                        (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")))
            base = current_app.config["PUBLIC_URL"] or request.host_url.rstrip("/")
            send_mail(u["email"], "TeacherAI — восстановление доступа",
                      f"Для смены пароля перейдите по ссылке (действует 1 час):\n{base}{url_for('auth.reset', token=token)}\n\n"
                      "Если вы не запрашивали смену пароля, просто проигнорируйте письмо.")
        sent = True
    return render_template("auth/forgot.html", sent=sent, mail_ok=mail_ok)


@bp.route("/reset/<token>", methods=["GET", "POST"])
@rate_limit("reset", 10, 3600)
def reset(token):
    h = hashlib.sha256(token.encode()).hexdigest()
    row = db.query("SELECT * FROM password_resets WHERE token_hash=? AND used=0", (h,), one=True)
    valid = row and row["expires_at"] >= datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    error = None
    if valid and request.method == "POST":
        pw = request.form.get("password", "")
        error = check_password_strength(pw) or (None if pw == request.form.get("password2") else "Пароли не совпадают.")
        if not error:
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), row["user_id"]))
            db.execute("UPDATE password_resets SET used=1 WHERE user_id=?", (row["user_id"],))
            flash("Пароль изменён. Войдите с новым паролем.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", valid=bool(valid), error=error)


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    msg = err = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "name":
            name = " ".join(request.form.get("name", "").split())[:80]
            if len(name) < 2:
                err = "Укажите имя (от 2 символов)."
            else:
                db.execute("UPDATE users SET name=? WHERE id=?", (name, g.user["id"]))
                msg = "Имя обновлено."
        elif action == "password":
            if not check_password_hash(g.user["password_hash"], request.form.get("current", "")):
                err = "Текущий пароль указан неверно."
            else:
                err = check_password_strength(request.form.get("password", "")) or \
                    (None if request.form.get("password") == request.form.get("password2") else "Пароли не совпадают.")
                if not err:
                    db.execute("UPDATE users SET password_hash=? WHERE id=?",
                               (generate_password_hash(request.form["password"]), g.user["id"]))
                    msg = "Пароль изменён."
        if msg:
            g.user = db.query("SELECT * FROM users WHERE id=?", (g.user["id"],), one=True)
    return render_template("teacher/profile.html", msg=msg, err=err)


@bp.route("/profile/delete", methods=["POST"])
@login_required
def delete_account():
    if g.user["role"] == "admin":
        flash("Аккаунт администратора удаляется только через серверную команду.", "error")
        return redirect(url_for("auth.profile"))
    if not check_password_hash(g.user["password_hash"], request.form.get("password", "")):
        flash("Пароль указан неверно — аккаунт не удалён.", "error")
        return redirect(url_for("auth.profile"))
    db.execute("DELETE FROM users WHERE id=?", (g.user["id"],))  # каскадно удаляет генерации и материалы
    session.clear()
    flash("Аккаунт и все связанные данные удалены.", "success")
    return redirect(url_for("public.index"))
