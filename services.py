"""Бизнес-логика: валидация, запуск генерации, нормализация и проверка результата."""
import ast
import json
import operator
import time

from . import ai, db
from .catalog import DIFFICULTIES, DURATIONS, GRADING, LETTERS, SUBJECTS, TASK_KINDS, WORK_TYPES


class ValidationError(Exception):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(errors.values()))


class GenerationError(Exception):
    pass


def _int(v, lo, hi):
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _text(v, maxlen):
    return " ".join(str(v or "").split())[:maxlen]


def _common(form, cfg, errors):
    p = {}
    subj = _text(form.get("subject"), 60)
    if subj == "__other":
        subj = _text(form.get("subject_other"), 60)
    if not subj:
        errors["subject"] = "Выберите предмет."
    p["subject"] = subj
    p["grade"] = _int(form.get("grade"), 1, 11)
    if p["grade"] is None:
        errors["grade"] = "Выберите класс от 1 до 11."
    letter = _text(form.get("class_letter"), 20)
    if letter == "__other":
        letter = _text(form.get("class_letter_other"), 3)
    letter = letter[:3]
    if not letter:
        errors["class_letter"] = "Укажите букву класса."
    p["class_letter"] = letter.upper()
    p["topic"] = _text(form.get("topic"), 200)
    if len(p["topic"]) < 3:
        errors["topic"] = "Укажите тему (не короче 3 символов)."
    p["wishes"] = _text(form.get("wishes"), 1000)
    p["goals"] = _text(form.get("goals"), 1000)
    return p


def validate(gtype, form, cfg):
    errors = {}
    p = _common(form, cfg, errors)
    p["type"] = gtype
    if gtype == "lesson":
        p["duration_minutes"] = _int(form.get("duration_minutes"), 10, 180)
        if p["duration_minutes"] is None:
            errors["duration_minutes"] = "Продолжительность: от 10 до 180 минут."
        p.update(variants_count=None, tasks_count=None, difficulty=None)
    elif gtype == "test":
        p["variants_count"] = _int(form.get("variants_count"), 1, 5)
        p["tasks_count"] = _int(form.get("tasks_count"), 1, 40)
        p["options_count"] = _int(form.get("options_count"), 2, 6)
        p["difficulty"] = form.get("difficulty") if form.get("difficulty") in DIFFICULTIES else None
        p["duration_minutes"] = _int(form.get("duration_minutes") or 20, 5, 180)
        for k, msg in (("variants_count", "Вариантов: от 1 до 5."), ("tasks_count", "Вопросов: от 1 до 40."),
                       ("options_count", "Вариантов ответа: от 2 до 6."), ("difficulty", "Выберите сложность.")):
            if p[k] is None:
                errors[k] = msg
    else:
        p["type"] = form.get("work_type") if form.get("work_type") in ("control", "independent", "check") else None
        if p["type"] is None:
            errors["work_type"] = "Выберите формат работы."
        p["variants_count"] = _int(form.get("variants_count"), 1, 10)
        p["tasks_count"] = _int(form.get("tasks_count"), 1, 30)
        p["difficulty"] = form.get("difficulty") if form.get("difficulty") in DIFFICULTIES else None
        p["duration_minutes"] = _int(form.get("duration_minutes"), 5, 180)
        if p["variants_count"] is None:
            errors["variants_count"] = "Количество вариантов: от 1 до 10."
        if p["tasks_count"] is None:
            errors["tasks_count"] = "Заданий в варианте: от 1 до 30."
        if p["difficulty"] is None:
            errors["difficulty"] = "Выберите сложность."
        if p["duration_minutes"] is None:
            errors["duration_minutes"] = "Укажите продолжительность (5–180 минут)."
        p["max_score"] = _int(form.get("max_score"), 1, 200) if form.get("max_score") else None
        if form.get("max_score") and p["max_score"] is None:
            errors["max_score"] = "Максимальный балл: от 1 до 200."
        p["task_kinds"] = [k for k in form.getlist("task_kinds") if k in TASK_KINDS]
        p["grading"] = form.get("grading") if form.get("grading") in GRADING else "five"
        p["with_answers"] = form.get("with_answers") == "1"
        p["with_solutions"] = form.get("with_solutions") == "1"
        p["with_criteria"] = form.get("with_criteria") == "1"
    if p.get("variants_count") and p.get("tasks_count") and \
            p["variants_count"] * p["tasks_count"] > cfg["MAX_TASKS_TOTAL"]:
        errors["tasks_count"] = f"Слишком большой объём: не более {cfg['MAX_TASKS_TOTAL']} заданий суммарно."
    if errors:
        raise ValidationError(errors)
    return p


# ---------- Промпты ----------
SYSTEM = ("Ты — опытный методист и учитель российской школы. Готовишь материалы по ФГОС для учителей. "
          "Пиши по-русски, корректно и без ошибок. Не выдумывай ссылки на учебники, номера страниц и "
          "нормативные документы. Все вычисления выполняй тщательно. Отвечай СТРОГО одним JSON-объектом, "
          "без пояснений и без markdown-обрамления.")


def build_prompt(p):
    base = (f"Предмет: {p['subject']}. Класс: {p['grade']}{p['class_letter']}. Тема: {p['topic']}.\n")
    if p["goals"]:
        base += f"Учебные цели: {p['goals']}\n"
    if p["wishes"]:
        base += f"Пожелания учителя: {p['wishes']}\n"
    if p["type"] == "lesson":
        return base + (
            f"Продолжительность урока: {p['duration_minutes']} минут.\n"
            "Составь подробный план урока. Верни JSON вида:\n"
            '{"title":"","goals":["..."],"stages":[{"name":"","minutes":5,"description":""}],'
            '"explanation":"объяснение материала","examples":["..."],"questions":["..."],'
            '"practice":[{"text":"","answer":""}],"teacher_notes":"ответы и пояснения для учителя",'
            '"homework":"","summary":"краткий итог урока"}\n'
            "Сумма минут по этапам должна равняться продолжительности урока.")
    diff = DIFFICULTIES[p["difficulty"]]
    spec = (f"Формат: {WORK_TYPES[p['type']]}. Вариантов: {p['variants_count']}. Заданий в каждом варианте: "
            f"{p['tasks_count']}. Сложность: {diff}. Время: {p['duration_minutes']} минут.\n"
            "Варианты должны быть равнозначными по сложности и структуре, но с разными числами/текстами/примерами — "
            "не копиями. Задания строго по теме и уровню класса.\n")
    if p["type"] == "test":
        return base + spec + (
            f"У каждого вопроса ровно {p['options_count']} вариантов ответа, правильный ровно один "
            "(индекс с нуля в поле correct). Распределяй позиции правильных ответов равномерно. Верни JSON:\n"
            '{"title":"","variants":[{"tasks":[{"text":"","options":["..."],"correct":0,"solution":"краткое пояснение",'
            '"check":null}]}],"criteria":["..."]}\n'
            'Для расчётных вопросов поле check = {"expr":"арифметическое выражение","value":число} для автопроверки, иначе null.')
    if p["task_kinds"]:
        spec += "Предпочтительные типы заданий: " + ", ".join(p["task_kinds"]) + ".\n"
    spec += (f"Система оценивания: {GRADING[p['grading']]}. "
             f"Максимальный балл за вариант: {p['max_score'] or p['tasks_count']}.\n")
    return base + spec + (
        "Верни JSON:\n"
        '{"title":"","variants":[{"tasks":[{"text":"условие","answer":"краткий ответ","solution":"подробное решение",'
        '"check":null}]}],"criteria":["критерии и шкала оценивания"]}\n'
        'Для расчётных заданий укажи check = {"expr":"арифметическое выражение с числами, + - * / ** и скобками","value":число} '
        "для автоматической проверки ответа, иначе null. Единицы измерения указывай в условии и ответе.")


# ---------- Безопасное вычисление для проверки ----------
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}


def safe_eval(expr):
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and (abs(b) > 12 or abs(a) > 1e6):
                raise ValueError("слишком большая степень")
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError("недопустимое выражение")
    return ev(ast.parse(str(expr).replace("^", "**").replace(",", "."), mode="eval").body)


def verify_check(check):
    """True/False — результат автопроверки, None — проверки нет."""
    if not isinstance(check, dict) or "expr" not in check or "value" not in check:
        return None
    try:
        got, want = float(safe_eval(check["expr"])), float(check["value"])
    except (ValueError, TypeError, ZeroDivisionError, SyntaxError, OverflowError):
        return False
    return abs(got - want) <= 1e-6 * max(1.0, abs(want))


# ---------- Очистка и нормализация содержимого ----------
def _s(v, n=4000):
    return str(v if v is not None else "").strip()[:n]


def _slist(v, n=60):
    if isinstance(v, str):
        v = [x for x in v.splitlines() if x.strip()]
    return [_s(x, 2000) for x in (v if isinstance(v, list) else [])][:n]


def clean_work(c, keep_meta=None):
    out = {"meta": keep_meta or c.get("meta", {}), "title": _s(c.get("title"), 200) or "Без названия",
           "criteria": _slist(c.get("criteria")), "variants": [], "warnings": _slist(c.get("warnings"), 30)}
    variants = c.get("variants")
    if not isinstance(variants, list):
        raise GenerationError("Неверная структура: нет списка вариантов.")
    for v in variants[:20]:
        tasks = []
        for t in (v.get("tasks") if isinstance(v, dict) else None) or []:
            if not isinstance(t, dict):
                continue
            try:
                pts = float(t.get("points", 1))
            except (TypeError, ValueError):
                pts = 1.0
            task = {"text": _s(t.get("text")), "answer": _s(t.get("answer"), 2000),
                    "solution": _s(t.get("solution"), 6000), "points": max(0.0, min(pts, 1000.0))}
            if isinstance(t.get("options"), list):
                task["options"] = [_s(o, 500) for o in t["options"]][:8]
                try:
                    task["correct"] = int(t.get("correct", 0))
                except (TypeError, ValueError):
                    task["correct"] = 0
                if not 0 <= task["correct"] < max(len(task["options"]), 1):
                    task["correct"] = 0
            if "verified" in t:
                task["verified"] = t["verified"] if isinstance(t["verified"], bool) else None
            tasks.append(task)
        if len(tasks) > 60:
            tasks = tasks[:60]
        variants_item = {"tasks": tasks}
        out["variants"].append(variants_item)
    return out


def clean_lesson(c, keep_meta=None):
    stages = []
    for s in (c.get("stages") if isinstance(c.get("stages"), list) else [])[:20]:
        if isinstance(s, dict):
            try:
                m = int(float(s.get("minutes", 0)))
            except (TypeError, ValueError):
                m = 0
            stages.append({"name": _s(s.get("name"), 200), "minutes": max(0, min(m, 180)),
                           "description": _s(s.get("description"), 4000)})
    practice = []
    for t in (c.get("practice") if isinstance(c.get("practice"), list) else [])[:40]:
        if isinstance(t, dict):
            practice.append({"text": _s(t.get("text")), "answer": _s(t.get("answer"), 2000)})
        elif isinstance(t, str):
            practice.append({"text": _s(t), "answer": ""})
    return {"meta": keep_meta or c.get("meta", {}), "title": _s(c.get("title"), 200) or "Урок",
            "goals": _slist(c.get("goals")), "stages": stages, "explanation": _s(c.get("explanation"), 20000),
            "examples": _slist(c.get("examples")), "questions": _slist(c.get("questions")),
            "practice": practice, "teacher_notes": _s(c.get("teacher_notes"), 10000),
            "homework": _s(c.get("homework"), 4000), "summary": _s(c.get("summary"), 4000),
            "warnings": _slist(c.get("warnings"), 30)}


def normalize_ai_result(data, p):
    """Строгая проверка результата AI относительно запрошенных параметров."""
    meta = {"kind": p["type"], "subject": p["subject"], "grade": p["grade"], "class_letter": p["class_letter"],
            "topic": p["topic"], "duration_minutes": p["duration_minutes"], "difficulty": p.get("difficulty")}
    if p["type"] == "lesson":
        c = clean_lesson(data, meta)
        if not c["stages"]:
            raise GenerationError("Модель не вернула этапы урока.")
        total = sum(s["minutes"] for s in c["stages"])
        if total != p["duration_minutes"]:
            c["warnings"].append(f"Сумма времени этапов ({total} мин) не равна длительности урока ({p['duration_minutes']} мин).")
        return c
    variants = data.get("variants")
    if not isinstance(variants, list) or len(variants) != p["variants_count"]:
        raise GenerationError(f"Модель вернула {len(variants) if isinstance(variants, list) else 0} вариантов вместо {p['variants_count']}.")
    is_test = p["type"] == "test"
    warnings = []
    for vi, v in enumerate(variants, 1):
        tasks = v.get("tasks") if isinstance(v, dict) else None
        if not isinstance(tasks, list) or len(tasks) != p["tasks_count"]:
            raise GenerationError(f"В варианте {vi} заданий {len(tasks) if isinstance(tasks, list) else 0} вместо {p['tasks_count']}.")
        for ti, t in enumerate(tasks, 1):
            if not isinstance(t, dict) or not _s(t.get("text")):
                raise GenerationError(f"Вариант {vi}, задание {ti}: пустая формулировка.")
            if is_test:
                opts = t.get("options")
                if not isinstance(opts, list) or len(opts) != p["options_count"]:
                    raise GenerationError(f"Вариант {vi}, вопрос {ti}: неверное число вариантов ответа.")
                if not isinstance(t.get("correct"), int) or not 0 <= t["correct"] < len(opts):
                    raise GenerationError(f"Вариант {vi}, вопрос {ti}: не указан правильный ответ.")
            ok = verify_check(t.get("check"))
            if ok is not None:
                t["verified"] = ok
                if not ok:
                    warnings.append(f"Вариант {vi}, задание {ti}: автопроверка вычислений не сошлась — проверьте ответ вручную.")
    c = clean_work({"title": data.get("title"), "variants": variants, "criteria": data.get("criteria")}, meta)
    c["warnings"] = warnings
    total = p.get("max_score") or p["tasks_count"]
    per = round(total / p["tasks_count"], 2)
    for v in c["variants"]:
        for t in v["tasks"]:
            t["points"] = per
            if not is_test:
                t.setdefault("verified", None)
            if not is_test and not p.get("with_answers", True):
                t["answer"] = ""
            if not is_test and not p.get("with_solutions", True):
                t["solution"] = ""
    meta["max_score"] = round(per * p["tasks_count"], 2)
    meta["grading"] = p.get("grading", "points")
    if not is_test and not p.get("with_criteria", True):
        c["criteria"] = []
    c["title"] = c["title"] if c["title"] != "Без названия" else f"{WORK_TYPES[p['type']]}: {p['topic']}"
    return c


# ---------- Запуск генерации ----------
def estimate_cost(cfg, tin, tout):
    if tin is None and tout is None:
        return None
    return round(((tin or 0) * cfg["AI_PRICE_INPUT"] + (tout or 0) * cfg["AI_PRICE_OUTPUT"]) / 1_000_000, 6)


def run_generation(app, user_id, p, request_token=None):
    """Создаёт запись, вызывает AI, сохраняет результат. Возвращает (generation_id, material_id|None)."""
    cfg = app.config
    if request_token:
        dup = db.query("SELECT id FROM generations WHERE user_id=? AND request_token=?", (user_id, request_token), one=True)
        if dup:  # повторная отправка той же формы — без нового списания лимита
            m = db.query("SELECT id FROM saved_materials WHERE generation_id=?", (dup["id"],), one=True)
            return dup["id"], (m["id"] if m else None)
    used = db.query("SELECT COUNT(*) c FROM generations WHERE user_id=? AND created_at >= datetime('now','-1 day')",
                    (user_id,), one=True)["c"]
    if used >= cfg["DAILY_LIMIT"]:
        raise GenerationError(f"Достигнут суточный лимит генераций ({cfg['DAILY_LIMIT']}). Попробуйте завтра.")
    provider = app.extensions.get("ai_provider")
    prompt = build_prompt(p)
    gid = db.execute(
        "INSERT INTO generations (user_id,type,subject,grade,class_letter,topic,difficulty,variants_count,tasks_count,"
        "duration_minutes,status,request_data,prompt_text,request_token,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, p["type"], p["subject"], p["grade"], p["class_letter"], p["topic"], p.get("difficulty"),
         p.get("variants_count"), p.get("tasks_count"), p.get("duration_minutes"), "pending",
         json.dumps(p, ensure_ascii=False), prompt, request_token, db.utcnow()))
    if provider is None:
        _fail(gid, "AI не настроен: задайте AI_API_KEY и AI_MODEL в окружении сервера.", None, 0)
        return gid, None
    db.execute("UPDATE generations SET status='running' WHERE id=?", (gid,))
    t0 = time.monotonic()
    res = None
    try:
        res = provider.complete(SYSTEM, prompt)
        data = ai.extract_json(res.text)
        content = normalize_ai_result(data, p)
    except (ai.AIError, GenerationError) as e:
        _fail(gid, str(e), res, int((time.monotonic() - t0) * 1000), cfg)
        return gid, None
    except Exception:  # непредвиденная ошибка не должна оставить запись «выполняется»
        app.logger.exception("generation %s crashed", gid)
        _fail(gid, "Внутренняя ошибка при генерации.", res, int((time.monotonic() - t0) * 1000), cfg)
        return gid, None
    ms = int((time.monotonic() - t0) * 1000)
    now = db.utcnow()
    db.execute(
        "UPDATE generations SET status='success', result_data=?, model_name=?, input_tokens=?, output_tokens=?, "
        "estimated_cost=?, duration_ms=?, completed_at=? WHERE id=?",
        (json.dumps(content, ensure_ascii=False), res.model or getattr(provider, "model", ""), res.input_tokens,
         res.output_tokens, estimate_cost(cfg, res.input_tokens, res.output_tokens), ms, now, gid))
    mid = db.execute("INSERT INTO saved_materials (user_id,generation_id,title,content,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                     (user_id, gid, content["title"], json.dumps(content, ensure_ascii=False), now, now))
    return gid, mid


def _fail(gid, msg, res, ms, cfg=None):
    db.execute("UPDATE generations SET status='error', error_message=?, duration_ms=?, completed_at=?, "
               "input_tokens=?, output_tokens=?, estimated_cost=?, model_name=? WHERE id=?",
               (msg, ms, db.utcnow(), getattr(res, "input_tokens", None), getattr(res, "output_tokens", None),
                estimate_cost(cfg, getattr(res, "input_tokens", None), getattr(res, "output_tokens", None)) if cfg and res else None,
                getattr(res, "model", None), gid))


# ---------- Ученическая версия ----------
SECRET_KEYS = {"answer", "solution", "correct", "check", "verified", "criteria", "teacher_notes", "warnings"}


def student_view(content):
    """Единая точка удаления ключей: используется экспортом и публичным прохождением теста."""
    out = {"meta": {k: v for k, v in content.get("meta", {}).items()}, "title": content.get("title", ""), "variants": []}
    for v in content.get("variants", []):
        tasks = []
        for t in v["tasks"]:
            nt = {k: val for k, val in t.items() if k not in SECRET_KEYS}
            tasks.append(nt)
        out["variants"].append({"tasks": tasks})
    return out
