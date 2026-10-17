import json

from flask import Blueprint, abort, g, redirect, render_template, request, url_for

from . import db
from .security import rate_limit
from .services import student_view

bp = Blueprint("public", __name__)


@bp.route("/")
def index():
    if g.user:
        return redirect(url_for("admin.dashboard" if g.user["role"] == "admin" else "teacher.dashboard"))
    return render_template("public/index.html")


@bp.route("/privacy")
def privacy():
    return render_template("public/privacy.html")


def _shared(token):
    row = db.query("SELECT s.id sid, s.active, m.content FROM shared_tests s JOIN saved_materials m ON m.id=s.material_id "
                   "WHERE s.token=?", (token,), one=True)
    if not row or not row["active"]:
        abort(404)
    return row, json.loads(row["content"])


@bp.route("/t/<token>")
@rate_limit("take", 60, 60, methods=("GET",))
def take_test(token):
    row, content = _shared(token)
    sv = student_view(content)  # ключ ответов физически удалён из данных страницы
    try:
        v = min(max(int(request.args.get("v", 1)), 1), len(sv["variants"]))
    except ValueError:
        v = 1
    return render_template("public/take.html", t=sv, v=v, token=token, result=None)


@bp.route("/t/<token>", methods=["POST"])
@rate_limit("take_submit", 20, 60)
def submit_test(token):
    row, content = _shared(token)
    try:
        v = int(request.form.get("v", 1))
        variant = content["variants"][v - 1]
    except (ValueError, IndexError):
        abort(400)
    name = " ".join(request.form.get("student_name", "").split())[:80]
    if len(name) < 2:
        sv = student_view(content)
        return render_template("public/take.html", t=sv, v=v, token=token, result=None, error="Укажите фамилию и имя.")
    score = maxs = 0.0
    details, answers = [], []
    for i, t in enumerate(variant["tasks"]):
        raw = request.form.get(f"q{i}")
        chosen = int(raw) if raw and raw.isdigit() and int(raw) < len(t["options"]) else None
        ok = chosen == t["correct"]
        maxs += t["points"]
        score += t["points"] if ok else 0
        answers.append(chosen)
        details.append({"text": t["text"], "options": t["options"], "chosen": chosen, "correct": t["correct"],
                        "ok": ok, "solution": t.get("solution", "")})
    db.execute("INSERT INTO test_attempts (shared_test_id,student_name,variant,answers,score,max_score,created_at) VALUES (?,?,?,?,?,?,?)",
               (row["sid"], name, v, json.dumps(answers), score, maxs, db.utcnow()))
    sv = student_view(content)
    return render_template("public/take.html", t=sv, v=v, token=token, error=None,
                           result={"score": score, "max": maxs, "details": details, "name": name})
