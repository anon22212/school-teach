import json
import secrets

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   Response, url_for)

from . import db, export
from .catalog import (DIFFICULTIES, DURATIONS, GRADES, GRADING, LETTERS, STATUSES, SUBJECTS, TASK_KINDS, WORK_TYPES)
from .security import login_required, rate_limit
from .services import GenerationError, ValidationError, clean_lesson, clean_work, run_generation, validate

bp = Blueprint("teacher", __name__)
PER_PAGE = 20


def _own_material(mid):
    m = db.query("SELECT * FROM saved_materials WHERE id=? AND user_id=?", (mid, g.user["id"]), one=True)
    if not m:
        abort(404)  # чужие материалы неотличимы от несуществующих
    return m


def _daily(uid, days=14):
    off = current_app.config["TZ_OFFSET_HOURS"]
    rows = db.query(f"SELECT strftime('%d.%m', datetime(created_at,'+{int(off)} hours')) d, COUNT(*) c FROM generations "
                    "WHERE user_id=? AND created_at >= datetime('now', ?) GROUP BY d ORDER BY MIN(created_at)",
                    (uid, f"-{days} days"))
    return [(r["d"], r["c"]) for r in rows]


@bp.route("/dashboard")
@login_required
def dashboard():
    uid = g.user["id"]
    stats = db.query("SELECT COUNT(*) total, SUM(status='success') ok, SUM(status='error') bad, "
                     "SUM(created_at >= datetime('now','-7 days')) week, SUM(type IN ('control','independent','check')) works, "
                     "SUM(type='test') tests, SUM(type='lesson') lessons FROM generations WHERE user_id=?", (uid,), one=True)
    saved = db.query("SELECT COUNT(*) c FROM saved_materials WHERE user_id=?", (uid,), one=True)["c"]
    recent = db.query("SELECT m.id, m.title, m.updated_at, g.type, g.subject, g.grade, g.class_letter FROM saved_materials m "
                      "LEFT JOIN generations g ON g.id=m.generation_id WHERE m.user_id=? ORDER BY m.updated_at DESC LIMIT 6", (uid,))
    failed = db.query("SELECT id, topic, created_at FROM generations WHERE user_id=? AND status='error' ORDER BY id DESC LIMIT 3", (uid,))
    daily = _daily(uid)
    mx = max([c for _, c in daily], default=1)
    return render_template("teacher/dashboard.html", stats=stats, saved=saved, recent=recent, failed=failed,
                           daily=[(d, c, round(c / mx * 100)) for d, c in daily], types=WORK_TYPES)


MODES = {"work": ("Создать контрольную", "work"), "test": ("Создать тест", "test"), "lesson": ("Подготовить урок", "lesson")}


@bp.route("/<mode>/new", methods=["GET", "POST"])
@login_required
@rate_limit("generate", 6, 60, per_user=True)
def generator(mode):
    if mode not in MODES:
        abort(404)
    gtype = {"work": "control", "test": "test", "lesson": "lesson"}[mode]
    errors, form = {}, request.form
    if request.method == "POST":
        try:
            p = validate(gtype, form, current_app.config)
        except ValidationError as e:
            errors = e.errors
        else:
            try:
                gid, mid = run_generation(current_app._get_current_object(), g.user["id"], p, form.get("request_token") or None)
            except GenerationError as e:
                flash(str(e), "error")
                return redirect(url_for("teacher.generator", mode=mode))
            if mid:
                flash("Материал готов! Проверьте и отредактируйте его при необходимости.", "success")
                return redirect(url_for("teacher.editor", mid=mid))
            return redirect(url_for("teacher.history_item", gid=gid))
    prefill = {}
    if request.method == "GET":
        src = request.args.get("from_lesson", type=int)
        if src:
            r = db.query("SELECT * FROM generations WHERE id=? AND user_id=?", (src, g.user["id"]), one=True)
            if r:
                prefill = {"subject": r["subject"], "grade": str(r["grade"]), "class_letter": r["class_letter"],
                           "topic": r["topic"], "goals": json.loads(r["request_data"]).get("goals", ""),
                           "duration_minutes": ""}
        form = prefill
    return render_template("teacher/generator.html", mode=mode, title=MODES[mode][0], errors=errors, form=form,
                           token=secrets.token_urlsafe(16), subjects=SUBJECTS, letters=LETTERS, grades=GRADES,
                           difficulties=DIFFICULTIES, durations=DURATIONS, task_kinds=TASK_KINDS, grading=GRADING,
                           work_types={k: v for k, v in WORK_TYPES.items() if k in ("control", "independent", "check")})


@bp.route("/history")
@login_required
def history():
    page = max(request.args.get("page", 1, type=int), 1)
    t = request.args.get("type", "")
    where, args = "user_id=?", [g.user["id"]]
    if t in WORK_TYPES:
        where += " AND type=?"
        args.append(t)
    total = db.query(f"SELECT COUNT(*) c FROM generations WHERE {where}", args, one=True)["c"]
    rows = db.query(f"SELECT g.*, (SELECT id FROM saved_materials WHERE generation_id=g.id) mid FROM generations g "
                    f"WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?", args + [PER_PAGE, (page - 1) * PER_PAGE])
    return render_template("teacher/history.html", rows=rows, page=page, pages=max((total + PER_PAGE - 1) // PER_PAGE, 1),
                           total=total, types=WORK_TYPES, statuses=STATUSES, cur_type=t)


@bp.route("/history/<int:gid>")
@login_required
def history_item(gid):
    r = db.query("SELECT * FROM generations WHERE id=? AND user_id=?", (gid, g.user["id"]), one=True)
    if not r:
        abort(404)
    m = db.query("SELECT id FROM saved_materials WHERE generation_id=?", (gid,), one=True)
    return render_template("teacher/history_item.html", r=r, mid=m["id"] if m else None, types=WORK_TYPES,
                           statuses=STATUSES, difficulties=DIFFICULTIES)


@bp.route("/materials")
@login_required
def materials():
    q = request.args.get("q", "").strip()[:80]
    where, args = "m.user_id=?", [g.user["id"]]
    if q:
        where += " AND (rulower(m.title) LIKE ? ESCAPE '\\' OR rulower(g.topic) LIKE ? ESCAPE '\\')"
        like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args += [like, like]
    rows = db.query("SELECT m.id, m.title, m.updated_at, g.type, g.subject, g.grade, g.class_letter FROM saved_materials m "
                    f"LEFT JOIN generations g ON g.id=m.generation_id WHERE {where} ORDER BY m.updated_at DESC LIMIT 200", args)
    return render_template("teacher/materials.html", rows=rows, q=q, types=WORK_TYPES)


@bp.route("/materials/<int:mid>")
@login_required
def editor(mid):
    m = _own_material(mid)
    content = json.loads(m["content"])
    share = db.query("SELECT * FROM shared_tests WHERE material_id=?", (mid,), one=True)
    attempts = db.query("SELECT a.* FROM test_attempts a JOIN shared_tests s ON s.id=a.shared_test_id WHERE s.material_id=? "
                        "ORDER BY a.id DESC LIMIT 100", (mid,)) if share else []
    base = current_app.config["PUBLIC_URL"] or request.host_url.rstrip("/")
    return render_template("teacher/editor.html", m=m, content=content, share=share, attempts=attempts, base=base,
                           types=WORK_TYPES)


@bp.route("/api/materials/<int:mid>", methods=["PUT"])
@login_required
def save_material(mid):
    m = _own_material(mid)
    old = json.loads(m["content"])
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Неверный формат данных."), 400
    try:
        new = clean_lesson(data, old["meta"]) if old["meta"].get("kind") == "lesson" else clean_work(data, old["meta"])
    except GenerationError as e:
        return jsonify(error=str(e)), 400
    if old["meta"].get("kind") == "test":
        for v in new["variants"]:
            for t in v["tasks"]:
                if len(t.get("options", [])) < 2:
                    return jsonify(error="В тесте у каждого вопроса должно быть не менее 2 вариантов ответа."), 400
    if old["meta"].get("kind") != "lesson":
        new["meta"]["max_score"] = max((round(sum(t["points"] for t in v["tasks"]), 2) for v in new["variants"]), default=0)
    now = db.utcnow()
    db.execute("INSERT INTO material_versions (material_id,content,created_at) VALUES (?,?,?)", (mid, m["content"], now))
    db.execute("DELETE FROM material_versions WHERE material_id=? AND id NOT IN "
               "(SELECT id FROM material_versions WHERE material_id=? ORDER BY id DESC LIMIT 50)", (mid, mid))
    db.execute("UPDATE saved_materials SET title=?, content=?, updated_at=? WHERE id=?",
               (new["title"], json.dumps(new, ensure_ascii=False), now, mid))
    return jsonify(ok=True, updated_at=now)


@bp.route("/materials/<int:mid>/delete", methods=["POST"])
@login_required
def delete_material(mid):
    _own_material(mid)
    db.execute("DELETE FROM saved_materials WHERE id=?", (mid,))
    flash("Материал удалён.", "success")
    return redirect(url_for("teacher.materials"))


def _created(m):
    from datetime import datetime, timedelta
    return (datetime.strptime(m["created_at"], "%Y-%m-%d %H:%M:%S") +
            timedelta(hours=current_app.config["TZ_OFFSET_HOURS"])).strftime("%d.%m.%Y")


@bp.route("/materials/<int:mid>/export/<kind>.<fmt>")
@login_required
def export_material(mid, kind, fmt):
    m = _own_material(mid)
    content = json.loads(m["content"])
    if kind not in ("student", "teacher") or fmt not in ("pdf", "docx"):
        abort(404)
    if content["meta"].get("kind") == "lesson":
        kind = "teacher"
    blks = export.blocks(content, kind, _created(m))
    data = export.to_pdf(blks) if fmt == "pdf" else export.to_docx(blks)
    mime = "application/pdf" if fmt == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    suffix = {"student": "ученик", "teacher": "ключ"}[kind] if content["meta"].get("kind") != "lesson" else "урок"
    from urllib.parse import quote
    fname = quote(f"{content['title'][:60]} ({suffix}).{fmt}")
    return Response(data, mimetype=mime, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"})


@bp.route("/materials/<int:mid>/print/<kind>")
@login_required
def print_material(mid, kind):
    m = _own_material(mid)
    content = json.loads(m["content"])
    if kind not in ("student", "teacher"):
        abort(404)
    if content["meta"].get("kind") == "lesson":
        kind = "teacher"
    return render_template("teacher/print.html", blocks=export.blocks(content, kind, _created(m)))


@bp.route("/materials/<int:mid>/share", methods=["POST"])
@login_required
def share(mid):
    m = _own_material(mid)
    content = json.loads(m["content"])
    if content["meta"].get("kind") != "test":
        abort(400)
    cur = db.query("SELECT * FROM shared_tests WHERE material_id=?", (mid,), one=True)
    action = request.form.get("action")
    if action == "enable":
        if cur:
            db.execute("UPDATE shared_tests SET active=1 WHERE id=?", (cur["id"],))
        else:
            db.execute("INSERT INTO shared_tests (material_id,token,active,created_at) VALUES (?,?,1,?)",
                       (mid, secrets.token_urlsafe(12), db.utcnow()))
        flash("Ссылка для прохождения включена.", "success")
    elif action == "disable" and cur:
        db.execute("UPDATE shared_tests SET active=0 WHERE id=?", (cur["id"],))
        flash("Ссылка отключена — тест снова личный.", "success")
    return redirect(url_for("teacher.editor", mid=mid))
