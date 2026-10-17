"""Админ-панель: все данные берутся из БД на сервере, каждый маршрут защищён admin_required."""
import json
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from . import db
from .catalog import DIFFICULTIES, GRADES, LETTERS, STATUSES, SUBJECTS, WORK_TYPES
from .security import admin_required

bp = Blueprint("admin", __name__, url_prefix="/admin")
PER_PAGE = 25
FMT = "%Y-%m-%d %H:%M:%S"
SORTS = {"created_at": "g.created_at", "user": "u.name", "subject": "g.subject", "grade": "g.grade",
         "class_letter": "g.class_letter", "topic": "g.topic", "type": "g.type", "status": "g.status",
         "duration": "g.duration_ms", "variants": "g.variants_count"}


def _off():
    return timedelta(hours=current_app.config["TZ_OFFSET_HOURS"])


def _day_start(days_back=0):
    local = datetime.now(timezone.utc).replace(tzinfo=None) + _off()
    return (local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_back) - _off()).strftime(FMT)


def _like(s):
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def filters(args):
    w, p = ["1=1"], []
    q = args.get("q", "").strip()[:100]
    if q:
        w.append("(rulower(u.name) LIKE ? ESCAPE '\\' OR rulower(u.email) LIKE ? ESCAPE '\\' OR rulower(g.topic) LIKE ? ESCAPE '\\' OR rulower(g.subject) LIKE ? ESCAPE '\\')")
        p += [_like(q.lower())] * 4
    period = args.get("period", "")
    if period == "today":
        w.append("g.created_at >= ?"); p.append(_day_start(0))
    elif period == "7":
        w.append("g.created_at >= ?"); p.append(_day_start(6))
    elif period == "30":
        w.append("g.created_at >= ?"); p.append(_day_start(29))
    elif period == "custom":
        for key, op, add in (("date_from", ">=", 0), ("date_to", "<", 1)):
            try:
                d = datetime.strptime(args.get(key, ""), "%Y-%m-%d") + timedelta(days=add)
                w.append(f"g.created_at {op} ?"); p.append((d - _off()).strftime(FMT))
            except ValueError:
                pass
    if args.get("subject"):
        w.append("g.subject=?"); p.append(args["subject"])
    if args.get("grade", "").isdigit():
        w.append("g.grade=?"); p.append(int(args["grade"]))
    if args.get("class_letter"):
        w.append("g.class_letter=?"); p.append(args["class_letter"].upper()[:3])
    if args.get("type") in WORK_TYPES:
        w.append("g.type=?"); p.append(args["type"])
    if args.get("status") in STATUSES:
        w.append("g.status=?"); p.append(args["status"])
    if args.get("user_id", "").isdigit():
        w.append("g.user_id=?"); p.append(int(args["user_id"]))
    return " AND ".join(w), p


def bars(rows, key_label=None):
    mx = max([v for _, v in rows], default=1) or 1
    return [(l, v, max(round(v / mx * 100), 3 if v else 0)) for l, v in rows]


def _daily(days=30, where="", args=()):
    off = int(current_app.config["TZ_OFFSET_HOURS"])
    rows = db.query(f"SELECT date(datetime(created_at,'+{off} hours')) d, COUNT(*) c FROM generations "
                    f"WHERE created_at >= ? {where} GROUP BY d", (_day_start(days - 1), *args))
    got = {r["d"]: r["c"] for r in rows}
    today = (datetime.now(timezone.utc).replace(tzinfo=None) + _off()).date()
    return [((today - timedelta(days=i)).strftime("%d.%m"), got.get((today - timedelta(days=i)).isoformat(), 0))
            for i in range(days - 1, -1, -1)]


def _audit(action, target=""):
    db.execute("INSERT INTO admin_audit (admin_id,action,target,created_at) VALUES (?,?,?,?)",
               (g.user["id"], action, target, db.utcnow()))


@bp.route("/")
@admin_required
def dashboard():
    one = lambda s, a=(): db.query(s, a, one=True)
    s = {
        "users": one("SELECT COUNT(*) c FROM users WHERE role='teacher'")["c"],
        "active": one("SELECT COUNT(*) c FROM users WHERE last_active_at >= datetime('now','-30 days')")["c"],
        "works": one("SELECT COUNT(*) c FROM generations WHERE type IN ('control','independent','check')")["c"],
        "tests": one("SELECT COUNT(*) c FROM generations WHERE type='test'")["c"],
        "lessons": one("SELECT COUNT(*) c FROM generations WHERE type='lesson'")["c"],
        "day": one("SELECT COUNT(*) c FROM generations WHERE created_at >= ?", (_day_start(0),))["c"],
        "week": one("SELECT COUNT(*) c FROM generations WHERE created_at >= ?", (_day_start(6),))["c"],
        "month": one("SELECT COUNT(*) c FROM generations WHERE created_at >= ?", (_day_start(29),))["c"],
    }
    subj = bars([(r["subject"], r["c"]) for r in db.query("SELECT subject, COUNT(*) c FROM generations GROUP BY subject ORDER BY c DESC LIMIT 8")])
    grades = bars([(f"{r['grade']} кл.", r["c"]) for r in db.query("SELECT grade, COUNT(*) c FROM generations GROUP BY grade ORDER BY grade")])
    recent = db.query("SELECT g.id, g.created_at, g.type, g.subject, g.grade, g.class_letter, g.topic, g.status, u.name uname "
                      "FROM generations g JOIN users u ON u.id=g.user_id ORDER BY g.id DESC LIMIT 10")
    return render_template("admin/dashboard.html", s=s, subj=subj, grades=grades, recent=recent,
                           daily=bars(_daily(30)), types=WORK_TYPES, statuses=STATUSES)


@bp.route("/generations")
@admin_required
def generations():
    where, args = filters(request.args)
    page = max(request.args.get("page", 1, type=int), 1)
    sort = request.args.get("sort", "created_at")
    sort = sort if sort in SORTS else "created_at"
    direction = "ASC" if request.args.get("dir") == "asc" else "DESC"
    total = db.query(f"SELECT COUNT(*) c FROM generations g JOIN users u ON u.id=g.user_id WHERE {where}", args, one=True)["c"]
    rows = db.query(
        f"SELECT g.id,g.created_at,g.type,g.subject,g.grade,g.class_letter,g.topic,g.difficulty,g.variants_count,g.tasks_count,"
        f"g.status,g.duration_ms,g.model_name,g.input_tokens,g.output_tokens,g.estimated_cost,u.id uid,u.name uname,u.email "
        f"FROM generations g JOIN users u ON u.id=g.user_id WHERE {where} ORDER BY {SORTS[sort]} {direction}, g.id DESC LIMIT ? OFFSET ?",
        args + [PER_PAGE, (page - 1) * PER_PAGE])
    users = db.query("SELECT id, name, email FROM users ORDER BY name")
    qs = {k: v for k, v in request.args.items() if k not in ("page",) and v}
    return render_template("admin/generations.html", rows=rows, total=total, page=page,
                           pages=max((total + PER_PAGE - 1) // PER_PAGE, 1), users=users, qs=qs, sort=sort,
                           direction=direction.lower(), types=WORK_TYPES, statuses=STATUSES, subjects=SUBJECTS,
                           letters=LETTERS, grades=GRADES, difficulties=DIFFICULTIES, a=request.args)


@bp.route("/generations/<int:gid>")
@admin_required
def generation(gid):
    r = db.query("SELECT g.*, u.name uname, u.email FROM generations g JOIN users u ON u.id=g.user_id WHERE g.id=?", (gid,), one=True)
    if not r:
        abort(404)
    _audit("view_generation", str(gid))
    mat = db.query("SELECT * FROM saved_materials WHERE generation_id=?", (gid,), one=True)
    versions = db.query("SELECT * FROM material_versions WHERE material_id=? ORDER BY id DESC", (mat["id"],)) if mat else []
    return render_template("admin/generation.html", r=r, mat=mat, versions=versions,
                           params=json.loads(r["request_data"]), result=json.loads(r["result_data"]) if r["result_data"] else None,
                           current=json.loads(mat["content"]) if mat else None,
                           types=WORK_TYPES, statuses=STATUSES, difficulties=DIFFICULTIES)


@bp.route("/users")
@admin_required
def users():
    q = request.args.get("q", "").strip()[:100]
    page = max(request.args.get("page", 1, type=int), 1)
    where, args = "1=1", []
    if q:
        where, args = "(rulower(u.name) LIKE ? ESCAPE '\\' OR rulower(u.email) LIKE ? ESCAPE '\\')", [_like(q.lower())] * 2
    total = db.query(f"SELECT COUNT(*) c FROM users u WHERE {where}", args, one=True)["c"]
    rows = db.query(f"SELECT u.*, (SELECT COUNT(*) FROM generations WHERE user_id=u.id) gens FROM users u WHERE {where} "
                    "ORDER BY u.created_at DESC LIMIT ? OFFSET ?", args + [PER_PAGE, (page - 1) * PER_PAGE])
    return render_template("admin/users.html", rows=rows, q=q, page=page, total=total,
                           pages=max((total + PER_PAGE - 1) // PER_PAGE, 1))


@bp.route("/users/<int:uid>")
@admin_required
def user(uid):
    u = db.query("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not u:
        abort(404)
    st = db.query("SELECT COUNT(*) total, SUM(status='success') ok, SUM(status='error') bad, COALESCE(SUM(estimated_cost),0) cost, "
                  "COALESCE(SUM(input_tokens),0) tin, COALESCE(SUM(output_tokens),0) tout FROM generations WHERE user_id=?", (uid,), one=True)
    rows = db.query("SELECT * FROM generations WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,))
    return render_template("admin/user.html", u=u, st=st, rows=rows, types=WORK_TYPES, statuses=STATUSES)


@bp.route("/users/<int:uid>/status", methods=["POST"])
@admin_required
def user_status(uid):
    u = db.query("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not u:
        abort(404)
    if u["id"] == g.user["id"] or u["role"] == "admin":
        flash("Нельзя менять доступ администраторов (включая свой).", "error")
        return redirect(url_for("admin.user", uid=uid))
    new = "suspended" if request.form.get("action") == "suspend" else "active"
    db.execute("UPDATE users SET status=? WHERE id=?", (new, uid))
    _audit("suspend_user" if new == "suspended" else "restore_user", str(uid))
    flash("Доступ приостановлен." if new == "suspended" else "Доступ восстановлен.", "success")
    return redirect(url_for("admin.user", uid=uid))


@bp.route("/analytics")
@admin_required
def analytics():
    one = lambda s, a=(): db.query(s, a, one=True)
    tot = one("SELECT COUNT(*) total, SUM(status='success') ok, SUM(status='error') bad, COALESCE(SUM(estimated_cost),0) cost, "
              "COALESCE(SUM(input_tokens),0) tin, COALESCE(SUM(output_tokens),0) tout, AVG(CASE WHEN status='success' THEN duration_ms END) avg_ms FROM generations")
    teachers = one("SELECT COUNT(*) c FROM users WHERE role='teacher'")["c"]
    month_cost = one("SELECT COALESCE(SUM(estimated_cost),0) c FROM generations WHERE created_at >= ?", (_day_start(29),))["c"]
    subj = bars([(r["subject"], r["c"]) for r in db.query("SELECT subject, COUNT(*) c FROM generations GROUP BY subject ORDER BY c DESC LIMIT 10")])
    grades = bars([(f"{r['grade']} кл.", r["c"]) for r in db.query("SELECT grade, COUNT(*) c FROM generations GROUP BY grade ORDER BY c DESC LIMIT 11")])
    topics = db.query("SELECT MIN(topic) topic, subject, COUNT(*) c FROM generations GROUP BY rulower(topic), subject ORDER BY c DESC, MAX(id) DESC LIMIT 10")
    models = db.query("SELECT COALESCE(model_name,'—') m, COUNT(*) c, COALESCE(SUM(estimated_cost),0) cost FROM generations GROUP BY m ORDER BY c DESC")
    return render_template("admin/analytics.html", tot=tot, avg=(tot["total"] / teachers if teachers else 0), teachers=teachers,
                           month_cost=month_cost, subj=subj, grades=grades, topics=topics, models=models,
                           daily=bars(_daily(30)))
