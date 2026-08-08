# Skills Required — Indigo Reconciliation System

This document lists the skills needed to understand, maintain, and extend this project.

---

## Must Have

### Python
- Python 3.12 syntax (type hints, `|` union types, f-strings)
- Virtual environments (`venv`)
- Async vs sync functions (FastAPI uses both)

### FastAPI
- Defining routes (`@router.get`, `@router.post`)
- Path and query parameters (`Path(...)`, `Query(...)`)
- Dependency injection (`Depends(get_db)`)
- Response models and status codes
- `StreamingResponse` for file downloads and SSE
- Background threads (`threading.Thread`)

### SQLAlchemy 2.x
- ORM model definition (`Mapped`, `mapped_column`)
- Session management (`Session`, `get_db`)
- Querying (`db.query(Model).filter(...).all()`)
- Core INSERT for bulk operations (`insert(Model)`)
- `func` for aggregations (`func.count`, `func.sum`, `func.json_search`)
- `or_` for OR conditions

### MySQL
- Basic SQL (SELECT, INSERT, UPDATE, JOIN)
- JSON column operations (`JSON_SEARCH`, `->>`)
- Understanding indexes (why `pnr`, `ticket_number` are indexed)

### pandas
- Reading Excel files (`pd.read_excel`)
- DataFrame operations (`.iterrows()`, `.dropna()`, `.iloc`)
- Data type handling (NaT, NaN, Timestamp)

### openpyxl
- Reading workbooks (`load_workbook`)
- Writing styled Excel output (fonts, fills, borders, merged cells)

---

## Good to Have

### Jinja2 Templates
- Template inheritance (`{% extends %}`, `{% block %}`)
- Loops and conditionals in templates
- Passing data from Python to HTML (`TemplateResponse`)

### JavaScript (Vanilla)
- `fetch` API for AJAX calls
- `EventSource` for SSE (Server-Sent Events)
- DOM manipulation
- `URLSearchParams` for building query strings

### Tailwind CSS
- Utility classes for layout and styling
- Understanding the design token classes used in this project (`text-on-surface`, `bg-primary`, etc.)

### Pydantic v2
- Schema definition (`BaseModel`, `Field`)
- Validators (`field_validator`)
- `model_validate` for ORM-to-schema conversion

---

## Domain Knowledge

### Airline Reconciliation Concepts
- **PNR** — Passenger Name Record, the 6-character booking reference (e.g. `KTCKMP`)
- **Gross Fare** — total amount charged to customer before deductions
- **Debit/Credit** — accounting direction of a transaction
- **Variance** — difference between cost and revenue for the same booking
- **Refund** — partial or full reversal of a payment

### Excel Structure of Client File
- The workbook has 7 sheets, each with different header row positions
- Title rows above the real header produce `Unnamed:` columns in pandas — handled by auto-detection
- Multiple tickets can be in a single cell (comma-separated) — handled by truncation

---

## Development Environment

| Tool | Version |
|---|---|
| Python | 3.12 |
| pip packages | See `requirements.txt` |
| Database | MySQL 8.x |
| OS | Linux |
| Port | 8000 (uvicorn) |

---

## Useful Commands

```bash
# Activate virtual environment
source venv/bin/activate

# Start development server
uvicorn app.main:app --reload --port 8000

# Create/recreate DB tables
python app/create_tables.py

# Check all registered routes
python -c "
from app.main import app
schema = app.openapi()
for path in sorted(schema['paths']):
    print(path)
"
```
