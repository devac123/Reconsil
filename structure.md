# Project Structure — Indigo Reconciliation System

```
rconsil/
├── app/
│   ├── main.py                          # FastAPI app, router registration, startup hook
│   ├── create_tables.py                 # One-shot table creation utility
│   │
│   ├── database/
│   │   ├── base.py                      # SQLAlchemy declarative base
│   │   ├── database.py                  # Engine + SessionLocal (MySQL connection)
│   │   ├── session.py                   # get_db() dependency for FastAPI
│   │   └── schema.py                    # ensure_schema() — CREATE TABLE + incremental ALTER TABLE
│   │
│   ├── models/                          # ORM table definitions
│   │   ├── __init__.py                  # Re-exports all models (ensures metadata is populated)
│   │   ├── organization.py              # organizations table
│   │   ├── upload_batch.py              # upload_batches table (multi-file upload groups)
│   │   ├── uploaded_file.py             # uploaded_files table + UploadStatus enum
│   │   ├── uploaded_sheet.py            # uploaded_sheets table (one row per sheet tab)
│   │   ├── staging_record.py            # staging_records table (raw Excel rows as JSON)
│   │   ├── reconciliation_result.py     # reconciliation_results table (one row per PNR)
│   │   └── reconciliation_remark.py     # reconciliation_remarks table (one row per label)
│   │
│   ├── schemas/                         # Pydantic schemas (placeholder, not yet used)
│   │   └── __init__.py
│   │
│   ├── repository/                      # Data access layer (DB queries only)
│   │   ├── __init__.py
│   │   ├── uploaded_file_repository.py
│   │   ├── uploaded_sheet_repository.py
│   │   └── staging_record_repository.py # bulk_create + paginated reads
│   │
│   ├── service/                         # Business logic layer
│   │   ├── __init__.py
│   │   ├── File_reader.py               # Excel → DataFrame (auto header detection, first 20 rows)
│   │   ├── progress_store.py            # In-memory job progress tracker for SSE
│   │   ├── organization_service.py      # Auto-detect org from filename
│   │   ├── uploaded_file_service.py     # Record upload metadata
│   │   ├── uploaded_sheet_service.py    # Detect and record sheet metadata
│   │   ├── staging_record_service.py    # Batch ingest Excel rows (500/commit) + SSE progress
│   │   └── reconciliation_service.py    # Core reconciliation engine (single + combined)
│   │
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── pages.py                     # Server-rendered HTML pages (Jinja2)
│   │   ├── api/
│   │   │   ├── __init__.py
│   │   │   ├── file_routes.py           # Upload endpoints (sync/async/multi) + SSE progress
│   │   │   ├── sheet_data_routes.py     # Staging data viewer with filters + pagination
│   │   │   ├── reconciliation_routes.py # Run reconciliation, results, summary, download
│   │   │   └── organization_routes.py   # Organization CRUD
│   │   └── web/
│   │       ├── __init__.py
│   │       └── views.py                 # (legacy, unused)
│   │
│   └── templates/                       # Jinja2 HTML templates
│       ├── base.html                    # Base layout (nav, Tailwind CDN)
│       ├── upload.html                  # File upload with live SSE progress bar
│       ├── dashboard.html               # Stats overview
│       ├── organizations.html           # Org list
│       ├── organization_detail.html     # Org detail + uploaded files
│       ├── uploaded_files.html          # All uploads (single + batch), filters
│       ├── sheets_data.html             # Sheet data viewer with filter/search bar
│       ├── index.html                   # (empty placeholder)
│       └── processing_result.html       # Post-processing result page
│
├── file/                                # Uploaded Excel files stored here
│   └── *.xlsx
│
├── context.md                           # Business context, data model, current status
├── structure.md                         # This file — directory tree + schema + API reference
├── readme.md                            # Setup and usage guide
├── skills.md                            # Skills needed to work on this project
├── requirements.txt                     # Python dependencies
├── logic.txt                            # Notes on reconciliation logic
├── debug_reconciliation.py              # Ad-hoc debug script
├── adminer.php                          # Adminer (MySQL web UI)
└── .gitignore
```

---

## Database Schema

```
organizations
├── id (PK)
├── name
└── created_at

upload_batches
├── id (PK)
├── organization_id (FK → organizations)
├── name
└── created_at

uploaded_files
├── id (PK)
├── organization_id  (FK → organizations)
├── batch_id         (FK → upload_batches, nullable — null for single-file uploads)
├── original_filename
├── stored_filename
├── file_path
├── file_size
├── file_extension
├── upload_status    [UPLOADED | PROCESSING | PROCESSED | FAILED]
├── uploaded_at
└── created_at / updated_at

uploaded_sheets
├── id (PK)
├── uploaded_file_id (FK → uploaded_files)
├── sheet_name
├── sheet_index
└── total_columns

staging_records
├── id (PK)
├── uploaded_sheet_id  (FK → uploaded_sheets)
├── row_number
├── pnr                (indexed — extracted from raw_data by _SHEET_FIELD_MAP)
├── ticket_number      (indexed)
├── transaction_date   (indexed, ISO-8601 string)
├── raw_data           (JSON — complete original row, all columns)
└── created_at / updated_at

reconciliation_results
├── id (PK)
├── uploaded_file_id   (FK → uploaded_files, CASCADE DELETE)
├── pnr                (indexed)
├── booking_date       (DATE, from AIR COST TRN BookingDate)
├── customer_name      (VARCHAR 255, from AIR COST TRN Name1)
├── cost_pnr  / cost_sale  / cost_refund  / cost_net
├── cashx_pnr / cashx_amount / cashx_refund / cashx_net
├── spyj_pnr  / spyj_amount  / spyj_refund  / spyj_net
├── variance           (cost_net − cashx_net − spyj_net)
├── remark             (comma-joined display string, e.g. "Not in SPYJ, Not in CASH X")
├── revised_remark     (nullable, for human override)
├── final_remark       (nullable)
└── created_at / updated_at

reconciliation_remarks
├── id (PK)
├── result_id  (FK → reconciliation_results, CASCADE DELETE)
└── remark     (indexed — individual label: "Matched", "Variance", "Not in Cost", etc.)
```

> One `ReconciliationResult` → many `ReconciliationRemark` rows.
> This allows a PNR missing from multiple sources to carry independent labels
> while still having a single result row.

---

## API Endpoints

### Files
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/files/upload` | Synchronous upload (no progress bar) |
| `POST` | `/files/upload-async` | Async upload — saves file, starts background thread, returns `job_id` |
| `POST` | `/files/upload-multiple-async` | Upload multiple workbooks — grouped under one `UploadBatch` |
| `GET`  | `/files/progress/{job_id}` | SSE stream — emits JSON progress events until `done` or `failed` |

### Sheet Data
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/files/{id}/sheets-data` | All sheets with filters + pagination |
| `GET` | `/files/{id}/sheets-data/{sheet_id}` | Single sheet rows with filters + pagination |

### Reconciliation
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/files/{id}/reconcile` | Run reconciliation for a single uploaded file |
| `POST` | `/files/reconcile-combined` | Run reconciliation across multiple files (body: `uploaded_file_ids`, `result_uploaded_file_id`) |
| `GET`  | `/files/{id}/reconcile/results` | Filtered, paginated result rows |
| `GET`  | `/files/{id}/reconcile/summary` | Counts + variance totals per remark label |
| `GET`  | `/files/{id}/reconcile/download` | Download colour-coded Excel output |

#### Result query filters (`/reconcile/results`)
| Param | Type | Description |
|---|---|---|
| `pnr` | string | Case-insensitive partial match |
| `remark` | string (repeatable) | Exact match on remark label (OR across values) |
| `variance_min` / `variance_max` | float | Variance range |
| `cost_filter` | `exist` \| `not_exist` | Presence in Cost source |
| `cashx_filter` | `exist` \| `not_exist` | Presence in CASH X source |
| `spyj_filter` | `exist` \| `not_exist` | Presence in SPYJ source |
| `comparison` | `matched` \| `variance` | Matched or Variance rows only |
| `page` / `page_size` | int | Pagination (max 500/page) |

### Organizations
| Method | Endpoint | Description |
|---|---|---|
| `GET`  | `/api/organizations` | List all orgs |
| `POST` | `/api/organizations` | Create org |
| `GET`  | `/api/organizations/{id}` | Get org detail |

### Pages (HTML)
| URL | Template | Description |
|---|---|---|
| `/` | — | Redirect → `/upload` |
| `/upload` | `upload.html` | File upload with live progress bar |
| `/dashboard` | `dashboard.html` | Stats: total orgs, files by status |
| `/organizations` | `organizations.html` | Org list |
| `/organizations/{id}` | `organization_detail.html` | Org detail + uploaded files |
| `/uploaded-files` | `uploaded_files.html` | All uploads (single + batch), filterable |
| `/sheets-data/{file_id}` | `sheets_data.html` | Sheet data viewer |
| `/processing-result/{file_id}` | `processing_result.html` | Post-processing result |
