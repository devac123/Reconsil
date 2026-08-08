# Indigo Reconciliation System

A web-based tool that automates the Cost vs Revenue reconciliation for Indigo airline bookings. Replaces manual Excel-based reconciliation with an automated engine that processes large files (60–70 MB, 3 lakh+ rows) and produces a colour-coded Excel output.

---

## Quick Start

### 1. Clone / navigate to the project

```bash
cd /var/www/html/rconsil
```

### 2. Create and activate virtual environment

```bash
python3.12 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure database

Edit `app/database/database.py` and set your MySQL connection:

```python
DATABASE_URL = "mysql+pymysql://user:password@localhost:3306/Rconsil"
```

### 5. Create database tables

```bash
python app/create_tables.py
```

### 6. Start the server

```bash
uvicorn app.main:app --reload --port 8000
```

### 7. Open in browser

```
http://localhost:8000
```

---

## Usage

### Step 1 — Upload a file

Go to `http://localhost:8000/upload`

- Drag and drop your Excel file (`.xlsx` / `.xls`)
- A live progress bar shows rows being imported in real time
- On success you'll see: Organization, File ID, Total Sheets, Rows Imported

### Step 2 — View sheet data

Go to `http://localhost:8000/sheets-data/{file_id}`

- Switch between sheet tabs
- Use the filter bar to search by PNR, ticket number, date range, or status
- Paginate through large sheets

### Step 3 — Run reconciliation

```
POST http://localhost:8000/files/{file_id}/reconcile
```

Or use the Swagger UI at `http://localhost:8000/docs`

### Step 4 — View results

```
# Summary counts per remark
GET http://localhost:8000/files/{file_id}/reconcile/summary

# Filter results
GET http://localhost:8000/files/{file_id}/reconcile/results?remark=Matched
GET http://localhost:8000/files/{file_id}/reconcile/results?variance_max=0
GET http://localhost:8000/files/{file_id}/reconcile/results?pnr=KTCKMP
```

### Step 5 — Download Excel output

```
GET http://localhost:8000/files/{file_id}/reconcile/download
```

Downloads a colour-coded Excel file mirroring the original Reconcilation sheet layout.

---

## Filter Reference

### Sheet Data Viewer (`/sheets-data/{file_id}`)

| Filter | Type | Description |
|---|---|---|
| `pnr` | string | Partial PNR match (searches all sheets) |
| `ticket_number` | string | Partial ticket number match |
| `date_from` | date | Transaction date from (YYYY-MM-DD) |
| `date_to` | date | Transaction date to (YYYY-MM-DD) |
| `is_processed` | bool | `true` or `false` |
| `page` | int | Page number (default: 1) |
| `page_size` | int | Rows per page (max: 500, default: 50) |

### Reconciliation Results (`/files/{id}/reconcile/results`)

| Filter | Type | Description |
|---|---|---|
| `pnr` | string | Partial PNR match |
| `remark` | string | Exact remark (Matched, Variance, Not in Cost, etc.) |
| `variance_min` | float | Minimum variance value |
| `variance_max` | float | Maximum variance value |
| `page` | int | Page number |
| `page_size` | int | Rows per page (max: 500) |

---

## Project Structure

See [structure.md](structure.md) for the full file/folder layout, database schema, and all API endpoints.

---

## Key Files

| File | Purpose |
|---|---|
| `app/service/reconciliation_service.py` | Core reconciliation engine |
| `app/service/staging_record_service.py` | Batch Excel row ingestion |
| `app/service/File_reader.py` | Auto header detection + DataFrame reader |
| `app/service/progress_store.py` | In-memory SSE progress tracker |
| `app/routes/api/file_routes.py` | Upload endpoints + SSE stream |
| `app/routes/api/reconciliation_routes.py` | Reconciliation endpoints |
| `app/routes/api/sheet_data_routes.py` | Sheet data + filter endpoints |
| `app/templates/upload.html` | Upload UI with live progress bar |
| `app/templates/sheets_data.html` | Sheet viewer with filter bar |

---

## Requirements

```
Python        3.12+
MySQL         8.0+
FastAPI
SQLAlchemy    2.x
pandas
openpyxl
PyMySQL
uvicorn
```

Full list in `requirements.txt`.

---

## API Documentation

Interactive Swagger UI available at:
```
http://localhost:8000/docs
```

---

## Notes

- Uploaded files are stored in the `file/` directory
- The reconciliation engine uses hardcoded column names for the current Indigo file format
- Dynamic column mapping (via the file_mappings table) is planned for future releases
- The progress bar uses Server-Sent Events (SSE) — requires a single uvicorn worker (not multi-worker gunicorn)
- For production with multiple workers, replace `progress_store.py` with a Redis-backed store

---

## Related Docs

- [context.md](context.md) — Business context and reconciliation logic
- [skills.md](skills.md) — Skills needed to work on this project
- [structure.md](structure.md) — Complete project structure and API reference
