# Project Context — Indigo Reconciliation System

## What Is This Project?

This is a **web-based reconciliation automation system** built for a travel agency (Innovations Solutions & Events) that manages Indigo airline bookings.

Previously, the reconciliation was done **manually in Excel** — comparing cost data against revenue data across multiple sheets to find variances. This system automates that entire process.

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
| **Reconcilation** | Manual output (reference) | `PNR` | `Cost-Cash X - SPYJ` |
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
| `abs(variance) ≈ 300 (±10)` | Markup/Booking Charges |
| PNR not in AIR COST TRN | Not in Cost |
| PNR not in CASH X | Not in CASH X |
| PNR not in SPYJ | Not in SPYJ |
| Any other variance | Variance |

> Note: The client's actual Excel uses more specific remarks (Excess Billing, Unbilled, Refund not issued, etc.). These will be made dynamic after client feedback.

---

## System Flow

```
1. User uploads Excel file (.xlsx)
          ↓
2. File saved to disk (/file/)
          ↓
3. Organization auto-detected from filename
          ↓
4. Sheet metadata recorded (uploaded_sheets table)
          ↓
5. All rows batch-imported into staging_records table
   (500 rows per commit, live progress bar via SSE)
          ↓
6. User triggers reconciliation
          ↓
7. Engine aggregates all 5 sheets by PNR
   Computes variance, assigns remarks
   Stores results in reconciliation_results table
          ↓
8. User downloads Excel output (colour-coded)
```

---

## Key Design Decisions

- **Staging table** — raw data stored as-is in JSON, not transformed. This preserves the original and allows re-processing.
- **Batch commits** — 500 rows per DB transaction to handle 1 lakh+ rows without memory issues.
- **SSE progress bar** — background thread processes the file, streams progress to browser in real time.
- **Auto header detection** — pandas scans first 20 rows to find the real header row, eliminating `Unnamed:` columns from title rows.
- **Hardcoded field map** — reconciliation engine uses hardcoded column names for now. Dynamic mapping via the file_mappings table is planned.
- **JSON search** — PNR filter searches both the indexed `pnr` column and raw_data JSON, so every sheet is searchable.

---

## Current Status

| Feature | Status |
|---|---|
| File upload with progress bar | ✅ Done |
| Auto header detection | ✅ Done |
| Batch row ingestion | ✅ Done |
| File mapping UI | ✅ Done |
| Reconciliation engine | ✅ Done |
| Excel download (styled) | ✅ Done |
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
