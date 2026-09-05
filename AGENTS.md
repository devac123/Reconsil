# Indigo Reconciliation System (rconsil)

FastAPI + Jinja2 + MySQL app that automates Cost-vs-Revenue reconciliation for Indigo airline bookings. Long-form domain notes live in `context.md`, `structure.md`, and `skills.md`.

## Stack

- Python 3.12, FastAPI, SQLAlchemy 2.x, PyMySQL / MySQL 8
- pandas + openpyxl for Excel ingest and styled download
- Server-rendered HTML (Jinja2, Tailwind CDN, vanilla JS)
- Run: `source venv/bin/activate && uvicorn app.main:app --reload --port 8000`

## Layout

- `app/main.py` — app, startup `ensure_schema()`, API routers then page router
- `app/routes/api/` — HTTP JSON; `app/routes/pages.py` — HTML
- `app/service/` — business logic; `app/repository/` — DB access only
- `app/models/` — ORM; `app/database/schema.py` — `create_all` + incremental ALTERs
- Uploads land in `file/`

## Layers

Keep routes thin. Put ingest/reconciliation in services. Add schema changes in both the model and `ensure_schema()`. Do not use `app/routes/web/views.py` (legacy unused).

## Jinja / Starlette

`TemplateResponse` is `templates.TemplateResponse(request, "name.html", context={...})`. The old `(template, {"request": request, ...})` form raises `TypeError` on Starlette 1.x.

## Do not

- Commit credentials, uploaded `.xlsx`, or `adminer.php`
- Change reconciliation formula or remark rules without matching `context.md` and `reconciliation_service.py`
- Put SQL in routes when a repository already exists
