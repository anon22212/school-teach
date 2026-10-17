# TeacherAI — AI-платформа для учителей

Генератор контрольных (несколько вариантов, ответы, решения, критерии), планы уроков, тесты с прохождением по ссылке,
редактор, экспорт PDF/DOCX/печать, личный кабинет и защищённая админ-панель `/admin` с журналом всех генераций
(кто, когда, предмет, класс, **буква класса**, тема, варианты). Интерфейс на русском, дизайн в 3D-стиле (светлая/тёмная тема).

## Стек
Python 3.10+, Flask, **SQLite (WAL) + SQL-миграции**, Jinja2/HTML/CSS/JS (без сборки), reportlab (PDF), python-docx (DOCX), gunicorn.
Зависимости — только 4 пакета (`requirements.txt`).

> **Важно про БД.** Эта сборка использует SQLite с собственным лёгким механизмом миграций (`migrations/*.sql`), без SQLAlchemy/PostgreSQL
> (на момент разработки не было доступа к установке пакетов, поэтому всё проверено на том, что реально запускалось). Для одного VPS
> и сотен учителей SQLite в режиме WAL достаточно. Переход на PostgreSQL потребует заменить слой `app/db.py` и SQL-диалект.

## Быстрый старт (локально)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # заполните SECRET_KEY и данные AI (см. ниже)
python manage.py init-db      # применить миграции
python manage.py create-admin # создать первого администратора (спросит email и пароль)
flask --app wsgi run          # http://127.0.0.1:5000
```
Без AI-ключа сайт запускается в **режиме настройки**: везде показывается баннер, генерация отключена, ничего не имитируется.

## Подключение AI
В `.env`:
```
AI_PROVIDER=anthropic        # или openai (любой OpenAI-совместимый API)
AI_API_KEY=...               # ключ только на сервере, в браузер не попадает
AI_MODEL=...                 # имя модели у вашего провайдера
AI_BASE_URL=                 # для openai-совместимых: например https://.../v1
AI_PRICE_INPUT_PER_MTOK=0    # цены за 1 млн токенов — для оценки расходов в аналитике
AI_PRICE_OUTPUT_PER_MTOK=0
```
Провайдер заменяется в `app/ai.py` (класс с методом `complete(system, user)`), остальной код не меняется.
Если нужен доступ из России, используйте `AI_PROVIDER=openai` с `AI_BASE_URL` доступного вам OpenAI-совместимого сервиса.

## Первый администратор
Роль `admin` выдаётся **только** серверной командой `python manage.py create-admin` (email/пароль из запроса или `ADMIN_EMAIL`/`ADMIN_PASSWORD`).
Публичная регистрация всегда создаёт роль `teacher`; роль проверяется по БД на каждом запросе.
Сброс пароля без почты: `python manage.py reset-password --email user@school.ru`.

## Тесты
```bash
python -m unittest discover -s tests -v
```
40 тестов: регистрация/вход, роли и закрытый `/admin`, чужие материалы, запись генераций (класс, буква, варианты), ошибки AI,
идемпотентность и лимиты, фильтры/поиск/сортировка журнала, PDF/DOCX, отсутствие ответов в ученической версии, тесты по ссылке,
перезапуск приложения, HTTP-код AI-провайдеров на локальном фейковом сервере. Тесты используют тестовый провайдер вместо реального AI.

## Production на VPS (Ubuntu)
```bash
sudo useradd -r -m teacherai && sudo mkdir -p /opt/teacherai && sudo chown teacherai: /opt/teacherai
# скопируйте проект в /opt/teacherai, затем под пользователем teacherai:
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # APP_ENV=production, SECRET_KEY, AI_*, PUBLIC_URL=https://ваш-домен
.venv/bin/python manage.py init-db && .venv/bin/python manage.py create-admin
sudo cp deploy/teacherai.service /etc/systemd/system/ && sudo systemctl enable --now teacherai
sudo cp deploy/nginx.conf /etc/nginx/sites-available/teacherai   # поправьте домен, включите, затем certbot --nginx
sudo cp deploy/backup.cron /etc/cron.d/teacherai-backup
```
В production без `SECRET_KEY` приложение не стартует; cookie помечаются `Secure`, нужен HTTPS.
Ошибки пишутся в `logs/app.log` (с ротацией), тексты запросов в логи не попадают. Резервные копии — `python manage.py backup` (папка `backups/`, хранится 14 последних).
`.env`, `instance/`, `logs/`, `backups/` исключены через `.gitignore`.

## Структура
```
app/            __init__ (фабрика, заголовки безопасности), config, db (миграции), security (CSRF, роли, лимиты),
                auth, teacher, admin, public, services (валидация, промпты, проверка вычислений, запуск генерации),
                ai (провайдеры), export (PDF/DOCX), catalog, templates/, static/ (css, js, шрифты DejaVu для PDF)
migrations/     001_init.sql   manage.py   wsgi.py   tests/   deploy/
```
Таблицы: users, generations (+индексы для поиска), saved_materials, material_versions, shared_tests, test_attempts, password_resets, admin_audit.

## Приватность и безопасность
Пароли — scrypt-хеш; сессии — подписанные HttpOnly/SameSite cookie; CSRF-токен на всех изменяющих запросах; CSP без inline-скриптов;
параметризованные SQL; экранирование в шаблонах; лимиты запросов (на процесс — для нескольких воркеров добавьте `limit_req` в nginx, он есть в примере);
суточный лимит генераций; идемпотентный токен формы против двойного списания. Админы видят параметры, текст запроса и результат генераций
(учителям об этом сообщается при регистрации, на страницах генераторов и на `/privacy`); просмотр записи админом фиксируется в `admin_audit`.

## Ограничения этой версии
Подписки/тарифы/платежи не реализованы (в админке отмечено); отправка писем восстановления требует SMTP (иначе — сброс командой);
математическая автопроверка работает для заданий, где модель вернула выражение `check`; остальные ответы проверяйте вручную.
