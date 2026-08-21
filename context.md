# Project Context — Indigo Reconciliation System

## What Is This Project?

A **web-based reconciliation automation system** built for a travel agency (Innovations Solutions & Events) that manages Indigo airline bookings.

Previously the reconciliation was done **manually in Excel** — comparing cost data against revenue data across multiple sheets to find variances. This system automates that entire process.

---

## The Business Problem

The client receives a large Excel file (60–70 MB, ~3 lakh+ rows) every period containing:

- What was **sold** to customers (Cash X Sale, SPYJ Sale)
- What **refunds** were issued (Cash X Re, SPJY Refund)
- What the **actual airline cost** was (AIR COST TRN)

The accountant had to manually cross-reference all 5 sheets by PNR (booking reference) to find:
- Which bookings match (variance < ₹1)
- Where there is overbilling or underbilling
- Where refunds were not issued
- Where cancellation charges were not recovered

This was error-prone and took hours. This system does it in seconds.

---

## Source Data (Excel Sheets)

| Sheet | Purpose | PNR Column | Amount Column |
|---|---|---|---|
| **AIR COST TRN** | Actual airline cost | `RecordLocator` | `PaymentAmount` (Debit/Credit) |
| **CASH x SAle** | Revenue — sales | `Formatted PNR` | `GROSS FARE` |
| **CASH X Re** | Revenue — refunds | `PNR formatted` | `GROSS FARE` |
| **SPYJ SALE** | Online sale cost | `GDS PNR` | `Total Amount` |
| **SPJY Refund** | Online refund cost | `GDS PNR` | `Total Refund Amount` |
| **Reconcilation** | Manual output (reference) | `PNR` | — |
| **Queries** | Manual notes | — | — |

---

## Reconciliation Formula

For each unique PNR across all sheets:

```
variance = cost_net - cashx_net - spyj_net

cost_net  = AIR COST TRN  (debit - credit)
cashx_net = CASH x SAle   - CASH X Re
spyj_net  = SPYJ SALE     - SPJY Refund
```

### Auto-assigned Remarks

| Condition | Remark |
|---|---|
| `abs(variance) < 1` | Matched |
| `abs(abs(variance) - 300) <= 10` | Markup/Booking Charges |
| PNR not in AIR COST TRN | Not in Cost |
| PNR not in CASH X | Not in CASH X |
| PNR not in SPYJ | Not in SPYJ |
| Any other variance | Variance |

Multiple missing-source labels are recorded individually (one row per label in `reconciliation_remarks`) and joined with ", " in the `remark` display column.

---

## System Flow

```
1. User uploads one or more Excel files (.xlsx)
          ↓
2. File(s) saved to disk (/file/)
          ↓
3. Organization auto-detected from filename
          ↓
4. Multi-file uploads: an UploadBatch record is created, all files linked to it
          ↓
5. Sheet metadata recorded (uploaded_sheets table)
          ↓
6. All rows batch-imported into staging_records table
   (500 rows per commit, live progress bar via SSE)
          ↓
7. User triggers reconciliation (single file or combined multi-file)
          ↓
8. Engine aggregates all 5 sheets by PNR
   Computes variance, assigns remarks
   Stores results in reconciliation_results table
   Stores individual remark labels in reconciliation_remarks table
          ↓
9. User downloads Excel output (colour-coded, styled)
```

---

## Key Design Decisions

- **Staging table** — raw data stored as-is in JSON (`raw_data` column), not transformed. Preserves the original and allows re-processing.
- **Batch commits** — 500 rows per DB transaction to handle 1 lakh+ rows without memory issues.
- **SSE progress bar** — background thread processes the file, streams JSON events to the browser in real time via `/files/progress/{job_id}`.
- **Auto header detection** — pandas scans first 20 rows to find the real header row, eliminating `Unnamed:` columns from title rows.
- **Hardcoded field map** — reconciliation engine uses hardcoded column names (defined in `_SHEET_FIELD_MAP` and aggregation methods). Dynamic mapping via the `file_mappings` table is planned but not yet active.
- **JSON search** — PNR filter searches both the indexed `pnr` column and raw_data JSON, so every sheet is searchable.
- **Normalised remarks** — each remark label is stored as an individual row in `reconciliation_remarks` (FK → `reconciliation_results`). The `remark` column on the result row is a comma-joined display cache. This supports multi-label rows (e.g. "Not in SPYJ, Not in CASH X") cleanly.
- **Multi-workbook reconciliation** — `reconcile_combined()` accepts a list of uploaded_file_ids and merges sheet data across workbooks before aggregating. Results are stored against a nominated `result_uploaded_file_id`.
- **UploadBatch** — multi-file uploads are grouped under a batch record so the UI can display them as one logical upload item.

---

## Database Models

| Model | Table | Purpose |
|---|---|---|
| `Organization` | `organizations` | Auto-detected client organisation |
| `UploadBatch` | `upload_batches` | Groups multi-file uploads |
| `UploadedFile` | `uploaded_files` | One record per uploaded workbook |
| `UploadedSheet` | `uploaded_sheets` | One record per sheet tab in a workbook |
| `StagingRecord` | `staging_records` | One row per Excel data row (raw JSON) |
| `ReconciliationResult` | `reconciliation_results` | One row per unique PNR after reconciliation |
| `ReconciliationRemark` | `reconciliation_remarks` | One row per remark label per result |

`UploadedFile` carries `upload_status` (`UPLOADED → PROCESSING → PROCESSED | FAILED`) and has a nullable `batch_id` FK to `UploadBatch`.

`ReconciliationResult` columns:
- `pnr`, `booking_date`, `customer_name` (from AIR COST TRN `BookingDate` / `Name1`)
- Cost side: `cost_pnr`, `cost_sale`, `cost_refund`, `cost_net`
- CASH X side: `cashx_pnr`, `cashx_amount`, `cashx_refund`, `cashx_net`
- SPYJ side: `spyj_pnr`, `spyj_amount`, `spyj_refund`, `spyj_net`
- `variance`, `remark`, `revised_remark`, `final_remark`

---

## API Endpoints

### File Upload
| Method | Path | Description |
|---|---|---|
| `POST` | `/files/upload` | Synchronous upload (no progress bar) |
| `POST` | `/files/upload-async` | Async upload — returns `job_id`, progress via SSE |
| `POST` | `/files/upload-multiple-async` | Upload multiple workbooks at once under one UploadBatch |
| `GET` | `/files/progress/{job_id}` | SSE stream — emits JSON progress events |

### Reconciliation
| Method | Path | Description |
|---|---|---|
| `POST` | `/files/{id}/reconcile` | Run reconciliation for a single uploaded file |
| `POST` | `/files/reconcile-combined` | Run reconciliation across multiple uploaded files (body: `uploaded_file_ids`, `result_uploaded_file_id`) |
| `GET` | `/files/{id}/reconcile/results` | Paginated, filtered result query |
| `GET` | `/files/{id}/reconcile/summary` | Counts and variance totals per remark |
| `GET` | `/files/{id}/reconcile/download` | Download colour-coded Excel output |

### Sheet Data
| Method | Path | Description |
|---|---|---|
| (see `sheet_data_routes.py`) | `/…` | Filtered paginated view of staging records |

### Organizations
| Method | Path | Description |
|---|---|---|
| (see `organization_routes.py`) | `/…` | CRUD for organisations |

### Page Routes (HTML)
| Path | Template |
|---|---|
| `/` | Redirect → `/upload` |
| `/upload` | `upload.html` |
| `/dashboard` | `dashboard.html` |
| `/organizations` | `organizations.html` |
| `/organizations/{id}` | `organization_detail.html` |
| `/uploaded-files` | `uploaded_files.html` |
| `/sheets-data/{file_id}` | `sheets_data.html` |
| `/processing-result/{file_id}` | `processing_result.html` |

---

## Excel Download Format

The downloaded reconciliation Excel has:
- Row 1: Title bar ("Indigo Reconciliation - Cost vs Revenue")
- Row 2: Source filename
- Row 3: Group headers (Parental PNR / Cost / CASH X / SPYJ Online Sale / VARIANCE / Remarks)
- Row 4: Column sub-headers (17 columns: A–Q)
- Row 5+: Data rows, colour-coded by remark

Colour scheme:
- Matched → light green
- Markup/Booking Charges → light yellow
- Not in Cost / Not in CASH X / Not in SPYJ → light orange
- Variance → light red

---

## Project Structure

```
app/
├── main.py                          # FastAPI app, router registration, startup hook
├── create_tables.py
├── database/
│   ├── base.py                      # SQLAlchemy declarative base
│   ├── database.py                  # Engine + SessionLocal
│   ├── session.py                   # get_db dependency
│   └── schema.py                    # ensure_schema() — CREATE TABLE + ALTER TABLE migrations
├── models/
│   ├── organization.py
│   ├── upload_batch.py
│   ├── uploaded_file.py             # UploadStatus enum: UPLOADED/PROCESSING/PROCESSED/FAILED
│   ├── uploaded_sheet.py
│   ├── staging_record.py
│   ├── reconciliation_result.py
│   └── reconciliation_remark.py
├── repository/
│   ├── staging_record_repository.py
│   ├── uploaded_file_repository.py
│   └── uploaded_sheet_repository.py
├── service/
│   ├── File_reader.py               # pandas-based sheet reader (auto header detection)
│   ├── progress_store.py            # In-memory job progress store for SSE
│   ├── organization_service.py
│   ├── uploaded_file_service.py
│   ├── uploaded_sheet_service.py
│   ├── staging_record_service.py    # Ingestion pipeline with batch commits + SSE progress
│   └── reconciliation_service.py   # Core reconciliation engine
├── routes/
│   ├── pages.py                     # Server-rendered HTML routes
│   └── api/
│       ├── file_routes.py           # Upload + SSE progress endpoints
│       ├── reconciliation_routes.py # Reconcile, query results, download
│       ├── sheet_data_routes.py     # Staging record viewer API
│       └── organization_routes.py
└── templates/
    ├── base.html
    ├── upload.html
    ├── uploaded_files.html
    ├── sheets_data.html
    ├── processing_result.html
    ├── dashboard.html
    ├── organizations.html
    └── organization_detail.html
```

---

## Current Status

| Feature | Status |
|---|---|
| File upload with SSE progress bar | ✅ Done |
| Multi-file upload (UploadBatch) | ✅ Done |
| Auto header detection | ✅ Done |
| Batch row ingestion (500-row commits) | ✅ Done |
| Reconciliation engine (single file) | ✅ Done |
| Combined multi-file reconciliation | ✅ Done |
| Normalised remarks table | ✅ Done |
| `booking_date` + `customer_name` on results | ✅ Done |
| Excel download (styled, colour-coded) | ✅ Done |
| Filtered + paginated result query API | ✅ Done |
| Reconciliation summary API | ✅ Done |
| Filtered sheet data viewer | ✅ Done |
| Reconciliation result filters | ✅ Done |
| Dynamic column mapping in reconciliation | ⏳ Pending client feedback |
| Parental PNR grouping | ⏳ Pending client feedback |
| Editable remarks in UI | ⏳ Pending client feedback |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI |
| Database | MySQL (via PyMySQL) |
| ORM | SQLAlchemy 2.x |
| Data processing | pandas, openpyxl |
| Frontend | Jinja2 templates, Tailwind CSS, vanilla JS |
| Server | Uvicorn |
