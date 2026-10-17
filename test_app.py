"""Автотесты TeacherAI. Запуск: python -m unittest discover -s tests -v"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash

from app import ai, create_app, db as database
from app.security import limiter
from app.services import safe_eval, verify_check


class FakeProvider:
    """Тестовый провайдер: возвращает корректный JSON под запрошенные параметры (НЕ используется в проде)."""
    name = "fake"
    model = "fake-model"

    def __init__(self):
        self.calls = 0
        self.mode = "ok"

    def complete(self, system, user):
        self.calls += 1
        if self.mode == "fail":
            raise ai.AIError("Тайм-аут: AI-провайдер не ответил вовремя.")
        if self.mode == "garbage":
            return ai.AIResult("это не JSON", 10, 5, self.model)
        nv = int(re.search(r"Вариантов: (\d+)", user).group(1)) if "Вариантов:" in user else 0
        nt = int(re.search(r"Заданий в каждом варианте: (\d+)", user).group(1)) if "Заданий в каждом" in user else 0
        if "Продолжительность урока" in user:
            data = {"title": "Урок", "goals": ["Цель"], "stages": [{"name": "Старт", "minutes": 45, "description": "d"}],
                    "explanation": "Объяснение", "examples": ["Пример"], "questions": ["Вопрос?"],
                    "practice": [{"text": "Задача", "answer": "42"}], "teacher_notes": "Заметки",
                    "homework": "Д/з", "summary": "Итог"}
        elif "ровно один" in user:
            nv = int(re.search(r"Вариантов: (\d+)", user).group(1))
            nt = int(re.search(r"Заданий в каждом варианте: (\d+)", user).group(1))
            no = int(re.search(r"ровно (\d+) вариантов ответа", user).group(1))
            data = {"title": "Тест", "variants": [{"tasks": [{"text": f"Вопрос {i}", "options": [f"o{j}" for j in range(no)],
                    "correct": 1, "solution": "Потому что", "check": None} for i in range(nt)]} for _ in range(nv)], "criteria": []}
        else:
            data = {"title": "Контрольная", "variants": [{"tasks": [
                {"text": f"Найдите ток при U={10 + v}, R=2", "answer": f"{(10 + v) / 2}", "solution": "I=U/R",
                 "check": {"expr": f"({10 + v})/2", "value": (10 + v) / 2}} for _ in range(nt)]} for v in range(nv)],
                "criteria": ["5 — все верно"]}
            if self.mode == "badmath":
                data["variants"][0]["tasks"][0]["check"] = {"expr": "2+2", "value": 5}
            if self.mode == "short":
                data["variants"] = data["variants"][:-1]
        return ai.AIResult(json.dumps(data, ensure_ascii=False), 1000, 2000, self.model)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.tmp, "t.db")
        self.cfg = dict(TESTING=True, DATABASE_PATH=self.dbpath, LOG_DIR=self.tmp, SECRET_KEY="test-secret",
                        AI_API_KEY="x", AI_MODEL="fake", RATE_LIMITS=False, DAILY_LIMIT=30,
                        AI_PRICE_INPUT=3.0, AI_PRICE_OUTPUT=15.0)
        self.make_app()

    def make_app(self):
        self.app = create_app(self.cfg)
        self.provider = FakeProvider()
        self.app.extensions["ai_provider"] = self.provider
        limiter.reset()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self):
        return self.app.test_client()

    @staticmethod
    def csrf(c, path="/login"):
        r = c.get(path)
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.get_data(as_text=True))
        return m.group(1)

    def register(self, c, name="Учитель 1", email="t1@example.com", pw="Passw0rdX"):
        tok = self.csrf(c, "/register")
        return c.post("/register", data=dict(csrf_token=tok, name=name, email=email, password=pw, password2=pw, agree="1"))

    def login(self, c, email, pw="Passw0rdX"):
        tok = self.csrf(c, "/login")
        return c.post("/login", data=dict(csrf_token=tok, email=email, password=pw))

    def make_admin(self, email="admin@example.com", pw="AdminPass1"):
        con = database.connect(self.dbpath)
        con.execute("INSERT INTO users (name,email,password_hash,role,status,created_at) VALUES ('Админ',?,?,'admin','active',?)",
                    (email, generate_password_hash(pw), database.utcnow()))
        con.commit(); con.close()
        return email, pw

    def work_form(self, c, **over):
        d = dict(csrf_token=self.csrf(c, "/work/new"), request_token=os.urandom(6).hex(), subject="Физика", grade="9",
                 class_letter="Б", topic="Закон Ома", work_type="control", variants_count="4", tasks_count="8",
                 difficulty="mixed", duration_minutes="45", with_answers="1", with_solutions="1", with_criteria="1")
        d.update(over)
        return d

    def q(self, sql, args=()):
        con = database.connect(self.dbpath)
        rows = con.execute(sql, args).fetchall()
        con.close()
        return rows


class AuthTests(Base):
    def test_register_login_logout(self):
        c = self.client()
        r = self.register(c)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get("/dashboard").status_code, 200)
        row = self.q("SELECT role, password_hash FROM users WHERE email='t1@example.com'")[0]
        self.assertEqual(row["role"], "teacher")
        self.assertNotIn("Passw0rdX", row["password_hash"])
        tok = self.csrf(c, "/dashboard")
        c.post("/logout", data={"csrf_token": tok})
        self.assertEqual(c.get("/dashboard").status_code, 302)
        r = self.login(c, "t1@example.com")
        self.assertEqual(r.status_code, 302)
        c2 = self.client()
        r = self.login(c2, "t1@example.com", "wrong-pass1")
        self.assertIn("Неверный email или пароль", r.get_data(as_text=True))

    def test_registration_validation_and_no_admin_escalation(self):
        c = self.client()
        tok = self.csrf(c, "/register")
        r = c.post("/register", data=dict(csrf_token=tok, name="X", email="bad", password="short", password2="short"))
        self.assertEqual(r.status_code, 200)
        c = self.client()
        tok = self.csrf(c, "/register")
        c.post("/register", data=dict(csrf_token=tok, name="Хакер", email="h@example.com", password="Passw0rdX",
                                      password2="Passw0rdX", agree="1", role="admin"))
        self.assertEqual(self.q("SELECT role FROM users WHERE email='h@example.com'")[0]["role"], "teacher")

    def test_csrf_required(self):
        c = self.client()
        r = c.post("/login", data=dict(email="a@b.ru", password="x"))
        self.assertEqual(r.status_code, 400)

    def test_suspended_user_cannot_login_or_use_session(self):
        c = self.client()
        self.register(c)
        self.q("SELECT 1")
        con = database.connect(self.dbpath)
        con.execute("UPDATE users SET status='suspended'"); con.commit(); con.close()
        self.assertEqual(c.get("/dashboard").status_code, 302)
        r = self.login(self.client(), "t1@example.com")
        self.assertIn("приостановлен", r.get_data(as_text=True))

    def test_delete_account_removes_data(self):
        c = self.client()
        self.register(c)
        c.post("/work/new", data=self.work_form(c, variants_count="1", tasks_count="2"))
        self.assertEqual(len(self.q("SELECT 1 FROM generations")), 1)
        tok = self.csrf(c, "/profile")
        c.post("/profile/delete", data=dict(csrf_token=tok, password="Passw0rdX"))
        self.assertEqual(len(self.q("SELECT 1 FROM users")), 0)
        self.assertEqual(len(self.q("SELECT 1 FROM generations")), 0)
        self.assertEqual(len(self.q("SELECT 1 FROM saved_materials")), 0)


class RoleTests(Base):
    def test_teacher_cannot_access_admin(self):
        c = self.client()
        self.register(c)
        for p in ("/admin/", "/admin/generations", "/admin/users", "/admin/analytics", "/admin/generations/1", "/admin/users/1"):
            self.assertEqual(c.get(p).status_code, 403, p)
        tok = self.csrf(c, "/dashboard")
        self.assertEqual(c.post("/admin/users/1/status", data=dict(csrf_token=tok, action="suspend")).status_code, 403)

    def test_anonymous_redirected_from_admin(self):
        r = self.client().get("/admin/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_admin_access_and_role_checked_in_db(self):
        email, pw = self.make_admin()
        c = self.client()
        self.login(c, email, pw)
        for p in ("/admin/", "/admin/generations", "/admin/users", "/admin/analytics"):
            self.assertEqual(c.get(p).status_code, 200, p)
        con = database.connect(self.dbpath)  # понижение роли действует немедленно
        con.execute("UPDATE users SET role='teacher' WHERE email=?", (email,)); con.commit(); con.close()
        self.assertEqual(c.get("/admin/").status_code, 403)

    def test_cannot_see_others_materials(self):
        a, b = self.client(), self.client()
        self.register(a, email="a@example.com")
        self.register(b, email="b@example.com", name="Учитель 2")
        a.post("/work/new", data=self.work_form(a, variants_count="1", tasks_count="2"))
        mid = self.q("SELECT id FROM saved_materials")[0]["id"]
        gid = self.q("SELECT id FROM generations")[0]["id"]
        self.assertEqual(a.get(f"/materials/{mid}").status_code, 200)
        self.assertEqual(b.get(f"/materials/{mid}").status_code, 404)
        self.assertEqual(b.get(f"/history/{gid}").status_code, 404)
        self.assertEqual(b.get(f"/materials/{mid}/export/student.pdf").status_code, 404)
        r = b.put(f"/api/materials/{mid}", json={"title": "x", "variants": []}, headers={"X-CSRFToken": self.csrf(b, "/dashboard")})
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("Закон Ома", b.get("/history").get_data(as_text=True))


class GenerationTests(Base):
    def test_generation_saved_with_class_letter_and_variants(self):
        c = self.client()
        self.register(c)
        r = c.post("/work/new", data=self.work_form(c))
        self.assertEqual(r.status_code, 302)
        g = self.q("SELECT * FROM generations")[0]
        self.assertEqual((g["subject"], g["grade"], g["class_letter"], g["topic"], g["variants_count"], g["tasks_count"], g["status"]),
                         ("Физика", 9, "Б", "Закон Ома", 4, 8, "success"))
        self.assertEqual(g["model_name"], "fake-model")
        self.assertEqual((g["input_tokens"], g["output_tokens"]), (1000, 2000))
        self.assertAlmostEqual(g["estimated_cost"], (1000 * 3 + 2000 * 15) / 1e6)
        res = json.loads(g["result_data"])
        self.assertEqual(len(res["variants"]), 4)
        self.assertTrue(all(len(v["tasks"]) == 8 for v in res["variants"]))
        self.assertEqual(len({v["tasks"][0]["text"] for v in res["variants"]}), 4)  # варианты не копии
        self.assertEqual(len({sum(t["points"] for t in v["tasks"]) for v in res["variants"]}), 1)
        self.assertIn("Закон Ома", g["prompt_text"])

    def test_required_fields_block_generation(self):
        c = self.client()
        self.register(c)
        for missing in ("subject", "grade", "class_letter", "topic", "variants_count"):
            r = c.post("/work/new", data=self.work_form(c, **{missing: ""}))
            self.assertEqual(r.status_code, 200, missing)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(len(self.q("SELECT 1 FROM generations")), 0)
        r = c.post("/work/new", data=self.work_form(c, variants_count="11"))
        self.assertEqual(r.status_code, 200)
        r = c.post("/work/new", data=self.work_form(c, class_letter="__other", class_letter_other="Э"))
        self.assertEqual(self.q("SELECT class_letter FROM generations")[0]["class_letter"], "Э")

    def test_ai_error_recorded(self):
        c = self.client()
        self.register(c)
        self.provider.mode = "fail"
        r = c.post("/work/new", data=self.work_form(c))
        self.assertEqual(r.status_code, 302)
        g = self.q("SELECT * FROM generations")[0]
        self.assertEqual(g["status"], "error")
        self.assertIn("Тайм-аут", g["error_message"])
        self.assertEqual(len(self.q("SELECT 1 FROM saved_materials")), 0)
        page = c.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Не удалось создать", page)

    def test_garbage_and_wrong_counts_are_errors(self):
        c = self.client()
        self.register(c)
        self.provider.mode = "garbage"
        c.post("/work/new", data=self.work_form(c))
        self.provider.mode = "short"
        c.post("/work/new", data=self.work_form(c))
        rows = self.q("SELECT status, error_message FROM generations ORDER BY id")
        self.assertEqual([r["status"] for r in rows], ["error", "error"])
        self.assertIn("вместо 4", rows[1]["error_message"])

    def test_no_api_key_setup_mode(self):
        self.app.extensions["ai_provider"] = None
        c = self.client()
        self.register(c)
        self.assertIn("Режим настройки", c.get("/dashboard").get_data(as_text=True))
        c.post("/work/new", data=self.work_form(c))
        g = self.q("SELECT status, error_message FROM generations")[0]
        self.assertEqual(g["status"], "error")
        self.assertIn("AI не настроен", g["error_message"])

    def test_idempotent_retry_does_not_double_count(self):
        c = self.client()
        self.register(c)
        form = self.work_form(c, variants_count="1", tasks_count="2")
        c.post("/work/new", data=form)
        c.post("/work/new", data=form)
        self.assertEqual(len(self.q("SELECT 1 FROM generations")), 1)
        self.assertEqual(self.provider.calls, 1)

    def test_daily_limit(self):
        self.cfg["DAILY_LIMIT"] = 2
        self.make_app()
        c = self.client()
        self.register(c)
        for _ in range(3):
            c.post("/work/new", data=self.work_form(c, variants_count="1", tasks_count="1"))
        self.assertEqual(len(self.q("SELECT 1 FROM generations")), 2)

    def test_math_verification(self):
        self.assertEqual(safe_eval("2+3*4"), 14)
        self.assertTrue(verify_check({"expr": "12/4", "value": 3}))
        self.assertFalse(verify_check({"expr": "12/4", "value": 4}))
        self.assertFalse(verify_check({"expr": "__import__('os').system('x')", "value": 0}))
        self.assertIsNone(verify_check(None))
        c = self.client()
        self.register(c)
        self.provider.mode = "badmath"
        c.post("/work/new", data=self.work_form(c, variants_count="1", tasks_count="2"))
        res = json.loads(self.q("SELECT result_data FROM generations")[0]["result_data"])
        self.assertIn("автопроверка", res["warnings"][0])
        self.assertIs(res["variants"][0]["tasks"][0]["verified"], False)

    def test_lesson_and_followup_prefill(self):
        c = self.client()
        self.register(c)
        tok = self.csrf(c, "/lesson/new")
        r = c.post("/lesson/new", data=dict(csrf_token=tok, subject="Физика", grade="9", class_letter="Б", topic="Закон Ома",
                                            duration_minutes="45", goals="Понять закон"))
        self.assertEqual(r.status_code, 302)
        g = self.q("SELECT * FROM generations")[0]
        self.assertEqual(g["type"], "lesson")
        page = c.get(f"/work/new?from_lesson={g['id']}").get_data(as_text=True)
        self.assertIn("Закон Ома", page)
        self.assertRegex(page, r'<option selected>Б</option>')

    def test_test_generator(self):
        c = self.client()
        self.register(c)
        tok = self.csrf(c, "/test/new")
        c.post("/test/new", data=dict(csrf_token=tok, subject="Физика", grade="9", class_letter="Б", topic="Закон Ома",
                                      variants_count="2", tasks_count="3", options_count="4", difficulty="basic"))
        res = json.loads(self.q("SELECT result_data FROM generations")[0]["result_data"])
        self.assertTrue(all(len(t["options"]) == 4 and t["correct"] == 1 for v in res["variants"] for t in v["tasks"]))


class EditorExportTests(Base):
    def setUp(self):
        super().setUp()
        self.c = self.client()
        self.register(self.c)
        self.c.post("/work/new", data=self.work_form(self.c, variants_count="2", tasks_count="3"))
        self.mid = self.q("SELECT id FROM saved_materials")[0]["id"]

    def test_edit_and_versions(self):
        content = json.loads(self.q("SELECT content FROM saved_materials")[0]["content"])
        content["title"] = "Моя контрольная"
        content["variants"][0]["tasks"][0]["text"] = "Новая формулировка"
        content["variants"][0]["tasks"].append({"text": "Добавленное", "answer": "1", "solution": "", "points": 1})
        content["variants"][1]["tasks"].pop()
        tok = self.csrf(self.c, "/dashboard")
        r = self.c.put(f"/api/materials/{self.mid}", json=content, headers={"X-CSRFToken": tok})
        self.assertEqual(r.status_code, 200, r.get_data())
        saved = json.loads(self.q("SELECT content FROM saved_materials")[0]["content"])
        self.assertEqual(saved["title"], "Моя контрольная")
        self.assertEqual(len(saved["variants"][0]["tasks"]), 4)
        self.assertEqual(saved["meta"]["class_letter"], "Б")
        self.assertEqual(len(self.q("SELECT 1 FROM material_versions")), 1)
        self.assertEqual(self.c.put(f"/api/materials/{self.mid}", json=content).status_code, 400)  # без CSRF

    def test_exports_pdf_docx(self):
        for kind in ("student", "teacher"):
            r = self.c.get(f"/materials/{self.mid}/export/{kind}.pdf")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.data.startswith(b"%PDF"))
            r = self.c.get(f"/materials/{self.mid}/export/{kind}.docx")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(zipfile.is_zipfile(io.BytesIO(r.data)))
        self.assertEqual(self.c.get(f"/materials/{self.mid}/export/student.exe").status_code, 404)

    def _docx_text(self, kind):
        from docx import Document
        d = Document(io.BytesIO(self.c.get(f"/materials/{self.mid}/export/{kind}.docx").data))
        return "\n".join(p.text for p in d.paragraphs)

    def test_student_version_has_no_answers(self):
        stu, tea = self._docx_text("student"), self._docx_text("teacher")
        self.assertNotIn("I=U/R", stu)
        self.assertNotIn("Ответ: 5.5", stu)
        self.assertNotIn("Критерии", stu)
        self.assertNotIn("5 — все верно", stu)
        self.assertIn("Фамилия, имя", stu)
        self.assertIn("I=U/R", tea)
        self.assertIn("5 — все верно", tea)
        html = self.c.get(f"/materials/{self.mid}/print/student").get_data(as_text=True)
        self.assertNotIn("I=U/R", html)
        self.assertNotIn("5 — все верно", html)

    def test_lesson_export(self):
        tok = self.csrf(self.c, "/lesson/new")
        self.c.post("/lesson/new", data=dict(csrf_token=tok, subject="Физика", grade="9", class_letter="Б", topic="Закон Ома", duration_minutes="45"))
        mid = self.q("SELECT id FROM saved_materials ORDER BY id DESC")[0]["id"]
        r = self.c.get(f"/materials/{mid}/export/teacher.pdf")
        self.assertTrue(r.data.startswith(b"%PDF"))


class PublicTestTests(Base):
    def test_take_test_hides_answers_until_finished(self):
        c = self.client()
        self.register(c)
        tok = self.csrf(c, "/test/new")
        c.post("/test/new", data=dict(csrf_token=tok, subject="Физика", grade="9", class_letter="Б", topic="Закон Ома",
                                      variants_count="1", tasks_count="3", options_count="3", difficulty="basic"))
        mid = self.q("SELECT id FROM saved_materials")[0]["id"]
        self.assertEqual(self.client().get("/t/nonexistent").status_code, 404)
        c.post(f"/materials/{mid}/share", data=dict(csrf_token=self.csrf(c, f"/materials/{mid}"), action="enable"))
        token = self.q("SELECT token FROM shared_tests")[0]["token"]
        s = self.client()
        page = s.get(f"/t/{token}").get_data(as_text=True)
        self.assertNotIn("Потому что", page)
        self.assertNotIn('"correct"', page)
        self.assertIn("Вопрос 0", page)
        ctok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        r = s.post(f"/t/{token}", data={"csrf_token": ctok, "v": "1", "student_name": "Иван Петров", "q0": "1", "q1": "0", "q2": "1"})
        body = r.get_data(as_text=True)
        self.assertIn("Потому что", body)
        a = self.q("SELECT * FROM test_attempts")[0]
        self.assertEqual((a["score"], a["max_score"]), (2.0, 3.0))
        c.post(f"/materials/{mid}/share", data=dict(csrf_token=self.csrf(c, f"/materials/{mid}"), action="disable"))
        self.assertEqual(self.client().get(f"/t/{token}").status_code, 404)


class AdminLogTests(Base):
    def setUp(self):
        super().setUp()
        t1, t2 = self.client(), self.client()
        self.register(t1, name="Анна", email="anna@example.com")
        self.register(t2, name="Борис", email="boris@example.com")
        t1.post("/work/new", data=self.work_form(t1, variants_count="2", tasks_count="2"))
        t1.post("/work/new", data=self.work_form(t1, subject="Математика", grade="5", class_letter="А", topic="Дроби", variants_count="1", tasks_count="2"))
        t2.post("/work/new", data=self.work_form(t2, subject="Математика", grade="7", class_letter="В", topic="Уравнения", variants_count="3", tasks_count="2"))
        self.provider.mode = "fail"
        t2.post("/work/new", data=self.work_form(t2, subject="Физика", grade="9", class_letter="Б", topic="Сбой"))
        self.admin = self.client()
        self.login(self.admin, *self.make_admin())

    def count(self, qs):
        page = self.admin.get("/admin/generations?" + qs).get_data(as_text=True)
        return len(re.findall(r'href="/admin/generations/\d+">Просмотр', page))

    def test_log_shows_all_with_user_and_class(self):
        page = self.admin.get("/admin/generations").get_data(as_text=True)
        self.assertEqual(self.count(""), 4)
        for s in ("Анна", "Борис", "Закон Ома", "anna@example.com"):
            self.assertIn(s, page)

    def test_filters_and_search(self):
        self.assertEqual(self.count("subject=Математика"), 2)
        self.assertEqual(self.count("grade=9"), 2)
        self.assertEqual(self.count("class_letter=Б"), 2)
        self.assertEqual(self.count("status=error"), 1)
        self.assertEqual(self.count("q=boris"), 2)
        self.assertEqual(self.count("q=Дроби"), 1)
        self.assertEqual(self.count("type=control"), 4)
        self.assertEqual(self.count("period=today"), 4)
        self.assertEqual(self.count("period=7"), 4)
        self.assertEqual(self.count("period=30&subject=Физика&class_letter=Б&grade=9"), 2)
        uid = self.q("SELECT id FROM users WHERE email='anna@example.com'")[0]["id"]
        self.assertEqual(self.count(f"user_id={uid}"), 2)
        self.assertEqual(self.count("period=custom&date_from=2000-01-01&date_to=2000-01-02"), 0)
        self.assertEqual(self.count("period=custom&date_from=2000-01-01&date_to=2999-01-01"), 4)
        self.assertEqual(self.count("q=%25"), 0)  # спецсимволы LIKE экранируются

    def test_sorting_and_pagination(self):
        page = self.admin.get("/admin/generations?sort=topic&dir=asc").get_data(as_text=True)
        self.assertLess(page.index("Дроби"), page.index("Уравнения"))
        self.assertEqual(self.admin.get("/admin/generations?sort=evil;DROP TABLE users").status_code, 200)
        self.assertEqual(self.admin.get("/admin/generations?page=99").status_code, 200)
        self.assertEqual(len(self.q("SELECT 1 FROM users")), 3)

    def test_detail_page_has_prompt_result_and_error(self):
        gid = self.q("SELECT id FROM generations WHERE topic='Закон Ома'")[0]["id"]
        page = self.admin.get(f"/admin/generations/{gid}").get_data(as_text=True)
        for s in ("Предмет: Физика", "Закон Ома", "Вариант 2", "I=U/R", "5 — все верно", "fake-model"):
            self.assertIn(s, page)
        gid = self.q("SELECT id FROM generations WHERE status='error'")[0]["id"]
        self.assertIn("Тайм-аут", self.admin.get(f"/admin/generations/{gid}").get_data(as_text=True))
        self.assertTrue(self.q("SELECT 1 FROM admin_audit WHERE action='view_generation'"))
        self.assertEqual(self.admin.get("/admin/generations/9999").status_code, 404)

    def test_dashboard_numbers_from_db(self):
        page = self.admin.get("/admin/").get_data(as_text=True)
        self.assertRegex(page, r"<b>2</b><span>Учителей зарегистрировано")
        self.assertRegex(page, r"<b>4</b><span>Контрольных и работ")
        self.assertIn("Математика", page)
        self.assertEqual(self.admin.get("/admin/analytics").status_code, 200)

    def test_user_management(self):
        uid = self.q("SELECT id FROM users WHERE email='boris@example.com'")[0]["id"]
        self.assertIn("Борис", self.admin.get("/admin/users?q=бор").get_data(as_text=True))
        tok = self.csrf(self.admin, f"/admin/users/{uid}")
        self.admin.post(f"/admin/users/{uid}/status", data=dict(csrf_token=tok, action="suspend"))
        self.assertEqual(self.q("SELECT status FROM users WHERE id=?", (uid,))[0]["status"], "suspended")
        self.assertIn("приостановлен", self.login(self.client(), "boris@example.com").get_data(as_text=True))
        self.admin.post(f"/admin/users/{uid}/status", data=dict(csrf_token=tok, action="restore"))
        self.assertEqual(self.q("SELECT status FROM users WHERE id=?", (uid,))[0]["status"], "active")
        me = self.q("SELECT id FROM users WHERE role='admin'")[0]["id"]
        self.admin.post(f"/admin/users/{me}/status", data=dict(csrf_token=tok, action="suspend"))
        self.assertEqual(self.q("SELECT status FROM users WHERE id=?", (me,))[0]["status"], "active")
        page = self.admin.get("/admin/users").get_data(as_text=True)
        self.assertNotIn("password_hash", page)
        self.assertNotIn("pbkdf2", page); self.assertNotIn("scrypt:", page)


class PersistenceAndMisc(Base):
    def test_data_survives_restart(self):
        c = self.client()
        self.register(c)
        c.post("/work/new", data=self.work_form(c, variants_count="2", tasks_count="2"))
        self.make_app()  # «перезапуск»: новый экземпляр приложения на той же БД
        c2 = self.client()
        self.assertEqual(self.login(c2, "t1@example.com").status_code, 302)
        page = c2.get("/history").get_data(as_text=True)
        self.assertIn("Закон Ома", page)
        self.assertEqual(len(self.q("SELECT 1 FROM schema_migrations")), 1)

    def test_rate_limit_login(self):
        self.cfg["RATE_LIMITS"] = True
        self.make_app()
        c = self.client()
        codes = []
        tok = self.csrf(c, "/login")
        for _ in range(12):
            codes.append(c.post("/login", data=dict(csrf_token=tok, email="a@b.ru", password="x")).status_code)
        self.assertIn(429, codes)

    def test_errors_and_headers(self):
        c = self.client()
        r = c.get("/no-such-page")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Страница не найдена", r.get_data(as_text=True))
        h = c.get("/login").headers
        self.assertIn("script-src 'self'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Frame-Options"], "DENY")

    def test_xss_escaped(self):
        c = self.client()
        self.register(c, name="<script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)", c.get("/dashboard").get_data(as_text=True))
        c.post("/work/new", data=self.work_form(c, topic="<img src=x onerror=alert(1)>", variants_count="1", tasks_count="1"))
        mid = self.q("SELECT id FROM saved_materials")[0]["id"]
        self.assertNotIn("<img src=x", c.get(f"/materials/{mid}").get_data(as_text=True))
        self.assertNotIn("<img src=x", c.get("/history").get_data(as_text=True))

    def test_password_reset_without_smtp_is_safe(self):
        c = self.client()
        r = c.post("/forgot", data=dict(csrf_token=self.csrf(c, "/forgot"), email="nobody@example.com"))
        self.assertIn("не настроена", r.get_data(as_text=True))

    def test_all_pages_render(self):
        c = self.client()
        for p in ("/", "/login", "/register", "/forgot", "/privacy"):
            self.assertEqual(c.get(p).status_code, 200, p)
        self.register(c)
        for p in ("/dashboard", "/work/new", "/test/new", "/lesson/new", "/history", "/materials", "/profile"):
            self.assertEqual(c.get(p).status_code, 200, p)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ProviderHTTPTests(unittest.TestCase):
    """Проверяет настоящий HTTP-код провайдеров на локальном фейковом сервере."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        cls.seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if "boom" in body.get("system", "") or any("boom" in str(m) for m in body.get("messages", [])):
                    self.send_response(500); self.end_headers(); self.wfile.write(b'{"error":"x"}'); return
                if self.path.endswith("/v1/messages"):
                    out = {"content": [{"type": "text", "text": '{"a":1}'}], "usage": {"input_tokens": 7, "output_tokens": 3}, "model": "m1"}
                else:
                    out = {"choices": [{"message": {"content": '{"a":2}'}}], "usage": {"prompt_tokens": 5, "completion_tokens": 4}, "model": "m2"}
                data = json.dumps(out).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)

        cls.srv = HTTPServer(("127.0.0.1", 0), H)
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_anthropic_provider(self):
        p = ai.AnthropicProvider("sk-secret", "model-x", self.url, timeout=5, max_tokens=100)
        r = p.complete("sys", "user")
        self.assertEqual((r.text, r.input_tokens, r.output_tokens, r.model), ('{"a":1}', 7, 3, "m1"))
        path, headers, body = self.seen[-1]
        self.assertEqual(headers["x-api-key"], "sk-secret")
        self.assertEqual((body["model"], body["system"]), ("model-x", "sys"))
        self.assertEqual(ai.extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_openai_compatible_provider(self):
        r = ai.OpenAICompatProvider("k", "m", self.url, timeout=5).complete("s", "u")
        self.assertEqual((r.text, r.input_tokens, r.output_tokens), ('{"a":2}', 5, 4))

    def test_http_error_and_unreachable_do_not_leak_key(self):
        p = ai.AnthropicProvider("sk-secret", "m", self.url, timeout=5)
        with self.assertRaises(ai.AIError) as cm:
            p.complete("boom", "x")
        self.assertIn("500", str(cm.exception))
        self.assertNotIn("sk-secret", str(cm.exception))
        with self.assertRaises(ai.AIError):
            ai.AnthropicProvider("k", "m", "http://127.0.0.1:1", timeout=2).complete("s", "u")

    def test_build_provider_requires_key_and_model(self):
        self.assertIsNone(ai.build_provider({"AI_API_KEY": "", "AI_MODEL": "m", "AI_PROVIDER": "anthropic", "AI_TIMEOUT": 1, "AI_MAX_TOKENS": 1}))
        self.assertIsNone(ai.build_provider({"AI_API_KEY": "k", "AI_MODEL": "", "AI_PROVIDER": "anthropic", "AI_TIMEOUT": 1, "AI_MAX_TOKENS": 1}))
        self.assertIsNotNone(ai.build_provider({"AI_API_KEY": "k", "AI_MODEL": "m", "AI_PROVIDER": "openai", "AI_BASE_URL": "", "AI_TIMEOUT": 1, "AI_MAX_TOKENS": 1}))
