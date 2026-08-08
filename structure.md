# Project Structure — Indigo Reconciliation System

```
rconsil/
├── app/
│   ├── main.py                          # FastAPI app, router registration
│   ├── create_tables.py                 # Create all DB tables (run once)
│   │
│   ├── database/
│   │   ├── base.py                      # SQLAlchemy declarative base
│   │   ├── database.py                  # Engine + SessionLocal (MySQL connection)
│   │   └── session.py                   # get_db() dependency for FastAPI
│   │
│   ├── models/                          # ORM table definitions
│   │   ├── organization.py              # organizations table
│   │   ├── uploaded_file.py             # uploaded_files table (+ UploadStatus enum)
│   │   ├── uploaded_sheet.py            # uploaded_sheets table
│   │   ├── staging_record.py            # staging_records table (raw Excel rows as JSON)
│   │   ├── file_mapping.py              # file_mappings table (+ MappingDataType enum)
│   │   ├── transaction.py               # transactions table
│   │   └── reconciliation_result.py     # reconciliation_results table
│   │
│   ├── schemas/
│   │   └── file_mapping.py              # Pydantic request/response schemas for mappings
│   │
│   ├── repository/                      # Data access layer (DB queries only)
│   │   ├── uploaded_file_repository.py
│   │   ├── uploaded_sheet_repository.py
│   │   ├── staging_record_repository.py # bulk_create + paginated reads
│   │   ├── file_mapping_repository.py
│   │   └── transaction_repository.py
│   │
│   ├── service/                         # Business logic layer
│   │   ├── File_reader.py               # Excel → DataFrame (auto header detection)
│   │   ├── organization_service.py      # Auto-detect org from filename
│   │   ├── uploaded_file_service.py     # Record upload metadata
│   │   ├── uploaded_sheet_service.py    # Detect and record sheet metadata
│   │   ├── staging_record_service.py    # Batch ingest Excel rows into staging
│   │   ├── file_mapping_service.py      # Save/retrieve column mappings
│   │   ├── transaction_service.py       # Transaction processing
│   │   ├── reconciliation_service.py    # Core reconciliation engine
│   │   └── progress_store.py            # In-memory job progress tracker (SSE)
│   │
│   ├── routes/
│   │   ├── pages.py                     # Server-rendered HTML pages (Jinja2)
│   │   ├── api/
│   │   │   ├── file_routes.py           # Upload (sync + async), SSE progress stream
│   │   │   ├── file_mapping_routes.py   # Column mapping CRUD + column discovery
│   │   │   ├── sheet_data_routes.py     # Staging data viewer with filters
│   │   │   ├── reconciliation_routes.py # Run reconciliation, results, summary, download
│   │   │   ├── organization_routes.py   # Organization CRUD
│   │   │   └── transaction_routes.py    # Transaction processing
│   │   └── web/
│   │       └── views.py                 # (legacy web views)
│   │
│   └── templates/                       # Jinja2 HTML templates
│       ├── base.html                    # Base layout (nav, styles)
│       ├── upload.html                  # File upload with live progress bar
│       ├── dashboard.html               # Stats overview
│       ├── organizations.html           # Org list
│       ├── organization_detail.html     # Org detail + files + mappings
│       ├── uploaded_files.html          # All uploaded files with filters
│       ├── sheets_data.html             # Sheet data viewer with filter bar
│       ├── file_mapping.html            # Column mapping UI
│       └── processing_result.html       # Post-processing result page
│
├── file/                                # Uploaded Excel files stored here
│   └── Indigo Reconciliation 2024-25.xlsx
│
├── context.md                           # Project context and business logic
├── skills.md                            # Skills needed to work on project
├── structure.md                         # This file
├── readme.md                            # Setup and usage guide
├── requirements.txt                     # Python dependencies
└── .gitignore
```

---

## Database Schema

```
organizations
├── id (PK)
├── name
└── created_at

uploaded_files
├── id (PK)
├── organization_id (FK → organizations)
├── original_filename
├── stored_path
├── upload_status  [UPLOADED | PROCESSING | PROCESSED | FAILED]
└── uploaded_at

uploaded_sheets
├── id (PK)
├── uploaded_file_id (FK → uploaded_files)
├── sheet_name
├── sheet_index
└── total_columns

staging_records
├── id (PK)
├── uploaded_sheet_id (FK → uploaded_sheets)
├── row_number
├── pnr          (indexed — extracted from raw_data by sheet field map)
├── ticket_number (indexed)
├── transaction_date (indexed)
├── raw_data     (JSON — complete original row)
├── is_processed (indexed)
└── created_at / updated_at

file_mappings
├── id (PK)
├── organization_id (FK → organizations)
├── sheet_name
├── excel_column
├── system_field
├── data_type
├── is_required
└── created_at / updated_at

reconciliation_results
├── id (PK)
├── uploaded_file_id (FK → uploaded_files)
├── pnr
├── cost_pnr / cost_sale / cost_refund / cost_net
├── cashx_pnr / cashx_amount / cashx_refund / cashx_net
├── spyj_pnr / spyj_amount / spyj_refund / spyj_net
├── variance
├── remark / revised_remark / final_remark
└── created_at / updated_at

transactions
├── id (PK)
├── uploaded_file_id (FK → uploaded_files)
└── (transaction fields)
```

---

## API Endpoints

### Files
| Method | Endpoint | Description |
|---|---|---|
| POST | `/files/upload` | Synchronous upload (no progress bar) |
| POST | `/files/upload-async` | Async upload — returns `job_id` immediately |
| GET | `/files/progress/{job_id}` | SSE stream for upload progress |

### Sheet Data
| Method | Endpoint | Description |
|---|---|---|
| GET | `/files/{id}/sheets-data` | All sheets with filters + pagination |
| GET | `/files/{id}/sheets-data/{sheet_id}` | Single sheet with filters + pagination |

### Reconciliation
| Method | Endpoint | Description |
|---|---|---|
| POST | `/files/{id}/reconcile` | Run reconciliation engine |
| GET | `/files/{id}/reconcile/results` | Filtered, paginated results |
| GET | `/files/{id}/reconcile/summary` | Counts + totals per remark |
| GET | `/files/{id}/reconcile/download` | Download styled Excel |

### File Mapping
| Method | Endpoint | Description |
|---|---|---|
| POST | `/file-mapping` | Save column mappings |
| GET | `/file-mapping/{org_id}` | List all mappings for org |
| GET | `/file-mapping/columns/{org_id}/{sheet}` | Discover columns from staging |

### Organizations
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/organizations` | List all orgs |
| POST | `/api/organizations` | Create org |
| GET | `/api/organizations/{id}` | Get org detail |

### Pages (HTML)
| URL | Page |
|---|---|
| `/upload` | File upload page |
| `/dashboard` | Dashboard |
| `/organizations` | Org list |
| `/uploaded-files` | All uploads |
| `/sheets-data/{file_id}` | Sheet data viewer |
| `/file-mapping-ui/{file_id}` | Column mapping UI |
