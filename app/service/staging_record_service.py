"""
StagingRecord Service
---------------------
Reads every data row from an Excel sheet via pandas and bulk-inserts them
into the ``staging_records`` staging table, one row per database record.

Design notes
~~~~~~~~~~~~
- All values are stored exactly as they appear in the workbook.
- Non-JSON-serialisable types (datetime, Decimal, numpy scalars, pandas NaT)
  are coerced to their string representation so the JSON column never errors.
- NaN / None values are stored as JSON ``null`` to preserve column presence.
- Each sheet is processed in BATCH_SIZE-row chunks (default 500).
  Each chunk is inserted and committed independently so:
    • Memory stays flat regardless of sheet size.
    • A failure mid-sheet rolls back only the current chunk; already-committed
      chunks are preserved and logged so the operator knows where to resume.
- Per-sheet PNR / ticket / date column names are resolved through
  ``_SHEET_FIELD_MAP`` so the staging table always has populated searchable
  fields regardless of the source sheet's naming conventions.
- ticket_number values that exceed the column length (100 chars) are
  truncated with a warning so a single long cell never aborts the entire
  import.
"""

import logging
import math
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Generator

import pandas as pd
from sqlalchemy.orm import Session

from app.models.uploaded_sheet import UploadedSheet
from app.repository.staging_record_repository import StagingRecordRepository
from app.service.File_reader import FileReaderService
from app.service import progress_store

logger = logging.getLogger(__name__)

# How many Excel rows to process and commit per batch.
# Keeping this at 500 matches the repository's internal DB chunk size and
# avoids building large in-memory lists before each commit.
BATCH_SIZE = 500

# Maximum character length for string columns in staging_records.
# Must match the String(n) lengths declared in the ORM model.
_MAX_PNR_LEN            = 100
_MAX_TICKET_NUMBER_LEN  = 100


# ---------------------------------------------------------------------------
# Per-sheet field-name mapping
# ---------------------------------------------------------------------------

_SHEET_FIELD_MAP: dict[str, dict[str, str | None]] = {
    "air cost trn": {
        "pnr":              "RecordLocator",
        "ticket_number":    None,
        "transaction_date": "Transaction Date",
    },
    "cash x sale": {
        "pnr":              "Formatted PNR",
        "ticket_number":    "TKT NO",
        "transaction_date": "D.O.T",
    },
    "cash x re": {
        "pnr":              "PNR formatted",
        "ticket_number":    "TKT NO",
        "transaction_date": "D.O.T",
    },
    "spyj sale": {
        "pnr":              "GDS PNR",
        "ticket_number":    "Ticket No",
        "transaction_date": "Booking Date",
    },
    "spjy refund": {
        "pnr":              "GDS PNR",
        "ticket_number":    "TicketNumbers",
        "transaction_date": "Refund Date and Time",
    },
    "reconcilation": {
        "pnr":              "PNR",
        "ticket_number":    None,
        "transaction_date": None,
    },
}

_DEFAULT_FIELD_MAP: dict[str, str | None] = {
    "pnr":              None,
    "ticket_number":    None,
    "transaction_date": None,
}


def _field_map_for(sheet_name: str) -> dict[str, str | None]:
    return _SHEET_FIELD_MAP.get(sheet_name.strip().lower(), _DEFAULT_FIELD_MAP)


# ---------------------------------------------------------------------------
# Value coercion helpers
# ---------------------------------------------------------------------------

def _is_null_like(value: Any) -> bool:
    """Return True for Python, pandas, and numpy missing scalar values."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    try:
        import pandas as _pd
        result = _pd.isna(value)
        if isinstance(result, bool):
            return result
        type_name = type(result).__module__
        if type_name == "numpy":
            return bool(getattr(result, "item", lambda: False)())
    except (ImportError, TypeError, ValueError):
        pass
    return False


def _make_json_safe(value: Any) -> Any:
    """Coerce *value* to a DB JSON-serialisable Python type."""
    if _is_null_like(value):
        return None
    try:
        import pandas as _pd
        if isinstance(value, _pd.Timestamp):
            return value.isoformat()
    except ImportError:
        pass
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    type_name = type(value).__module__
    if type_name == "numpy":
        return _make_json_safe(getattr(value, "item", lambda: value)())
    return value


def _row_to_dict(columns: list[str], values) -> dict:
    return {col: _make_json_safe(val) for col, val in zip(columns, values)}


def _to_date_str(value: Any) -> str | None:
    """Coerce *value* to an ISO-8601 date string or ``None``."""
    if _is_null_like(value):
        return None
    try:
        import pandas as _pd
        if isinstance(value, _pd.Timestamp):
            return value.date().isoformat()
    except ImportError:
        pass
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if len(s) >= 10 and s[4] == "-":
            return s[:10]
        return None
    type_name = type(value).__module__
    if type_name == "numpy":
        value = getattr(value, "item", lambda: value)()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            n = int(value)
            if 1 <= n <= 2958465:
                from datetime import date as _date, timedelta
                return (_date(1899, 12, 30) + timedelta(days=n)).isoformat()
        except (ValueError, OverflowError):
            pass
    return None


def _extract_field(raw_data: dict, column_name: str | None) -> Any:
    if not column_name:
        return None
    v = raw_data.get(column_name)
    if isinstance(v, str) and not v.strip():
        return None
    return v


def _extract_date_field(raw_data: dict, column_name: str | None) -> str | None:
    return _to_date_str(_extract_field(raw_data, column_name))


def _safe_str(value: Any, max_len: int, field_label: str, row_number: int) -> str | None:
    """
    Convert *value* to a string and truncate to *max_len* if necessary.

    When truncation occurs a WARNING is logged so the operator can identify
    rows where the value was clipped.  Returns ``None`` for null-like values.
    """
    if _is_null_like(value):
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) > max_len:
        logger.warning(
            "Row %s: %s value truncated from %s to %s chars: '%s...'",
            row_number, field_label, len(s), max_len, s[:30],
        )
        return s[:max_len]
    return s


# ---------------------------------------------------------------------------
# DataFrame chunk generator
# ---------------------------------------------------------------------------

def _iter_df_chunks(
    df: pd.DataFrame,
    chunk_size: int,
) -> Generator[pd.DataFrame, None, None]:
    """Yield successive *chunk_size*-row slices of *df*."""
    total = len(df)
    for start in range(0, total, chunk_size):
        yield df.iloc[start: start + chunk_size]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class StagingRecordService:
    """
    Business-logic layer for staging-record ingestion.

    Processing model
    ----------------
    For each sheet the service:

    1. Reads the full worksheet into a DataFrame once.
    2. Slices the DataFrame into BATCH_SIZE-row chunks.
    3. For each chunk:
       a. Converts rows to JSON-safe dicts.
       b. Truncates any string field that would exceed the column length.
       c. Calls the repository bulk_create (500-row DB inserts internally).
       d. Commits immediately so the DB receives data incrementally.
    4. If a chunk fails its commit is rolled back; already-committed chunks
       are preserved and the error is re-raised with context.
    """

    def __init__(self, db: Session) -> None:
        self._db = db
        self._repo = StagingRecordRepository(db)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def ingest_rows(
        self,
        file_path: str,
        sheets: list[UploadedSheet],
        batch_size: int = BATCH_SIZE,
        job_id: str | None = None,
        finalize_progress: bool = True,
    ) -> int:
        """
        Read every data row from the workbook and bulk-insert staging records
        in ``batch_size``-row chunks with per-chunk commits.

        Parameters
        ----------
        file_path : str
            Path to the Excel file on disk.
        sheets : list[UploadedSheet]
            Committed sheet records (must already have IDs).
        batch_size : int
            Rows per commit batch.  Defaults to ``BATCH_SIZE`` (500).
        job_id : str | None
            When provided, progress updates are pushed to the progress store
            after every committed batch so the SSE endpoint can stream them.
        finalize_progress : bool
            When ``True``, mark the progress job done after this workbook.
            Multi-workbook ingestion sets this to ``False`` until all files
            have been processed.

        Returns
        -------
        int
            Total number of staging rows inserted across all sheets.

        Raises
        ------
        FileNotFoundError
            If *file_path* does not exist.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(
                f"Cannot ingest rows: file not found at '{file_path}'."
            )

        # Pre-scan total rows across all sheets so we can show overall percent.
        total_rows_all_sheets = 0
        sheet_row_counts: list[int] = []
        for sheet in sheets:
            try:
                df_tmp = FileReaderService.read_sheet_as_dataframe(path, sheet.sheet_name)
                count  = len(df_tmp)
            except Exception:
                count = 0
            sheet_row_counts.append(count)
            total_rows_all_sheets += count

        if job_id:
            progress_store.update_job(
                job_id,
                status="processing",
                total_rows=total_rows_all_sheets,
                rows_done=0,
                percent=0,
                message="Starting ingestion…",
            )

        total_rows_inserted  = 0
        cumulative_rows_done = 0

        for sheet, sheet_total in zip(sheets, sheet_row_counts):
            sheet_rows = self._ingest_single_sheet(
                path, sheet, batch_size,
                job_id=job_id,
                cumulative_rows_done=cumulative_rows_done,
                total_rows_all_sheets=total_rows_all_sheets,
            )
            total_rows_inserted  += sheet_rows
            cumulative_rows_done += sheet_total

        if job_id and finalize_progress:
            progress_store.update_job(
                job_id,
                status="done",
                percent=100,
                rows_done=total_rows_inserted,
                message="Ingestion complete.",
            )

        logger.info(
            "Staging complete: %s total row(s) inserted from '%s'.",
            total_rows_inserted,
            path.name,
        )
        return total_rows_inserted

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    def _ingest_single_sheet(
        self,
        file_path: Path,
        sheet: UploadedSheet,
        batch_size: int,
        job_id: str | None = None,
        cumulative_rows_done: int = 0,
        total_rows_all_sheets: int = 0,
    ) -> int:
        """
        Read one worksheet and bulk-insert its rows in ``batch_size``-row
        batches, committing after each batch.

        Returns the total rows inserted for this sheet.
        """
        logger.info(
            "Reading sheet '%s' (id=%s) from '%s'.",
            sheet.sheet_name,
            sheet.id,
            file_path.name,
        )

        df: pd.DataFrame = FileReaderService.read_sheet_as_dataframe(
            file_path, sheet.sheet_name
        )

        if df.empty:
            logger.warning(
                "Sheet '%s' (id=%s) contains no data rows — skipping.",
                sheet.sheet_name,
                sheet.id,
            )
            return 0

        columns       = df.columns.tolist()
        field_map     = _field_map_for(sheet.sheet_name)
        total_rows    = len(df)
        total_batches = math.ceil(total_rows / batch_size)

        logger.info(
            "Sheet '%s' (id=%s): %s rows → %s batch(es) of %s.",
            sheet.sheet_name,
            sheet.id,
            total_rows,
            total_batches,
            batch_size,
        )

        sheet_inserted = 0

        for batch_num, chunk_df in enumerate(_iter_df_chunks(df, batch_size), start=1):
            row_offset = (batch_num - 1) * batch_size

            rows: list[dict] = []
            for local_idx, (_, row_series) in enumerate(chunk_df.iterrows()):
                row_number = row_offset + local_idx + 1
                raw_data   = _row_to_dict(columns, row_series)

                pnr_raw    = _extract_field(raw_data, field_map["pnr"])
                ticket_raw = _extract_field(raw_data, field_map["ticket_number"])

                rows.append({
                    "row_number":       row_number,
                    "pnr":              _safe_str(pnr_raw,    _MAX_PNR_LEN,           "pnr",           row_number),
                    "ticket_number":    _safe_str(ticket_raw, _MAX_TICKET_NUMBER_LEN, "ticket_number", row_number),
                    "transaction_date": _extract_date_field(raw_data, field_map["transaction_date"]),
                    "raw_data":         raw_data,
                })

            try:
                inserted = self._repo.bulk_create(
                    uploaded_sheet_id=sheet.id,
                    uploaded_file_id=sheet.uploaded_file_id,
                    rows=rows,
                )
                self._db.commit()
                sheet_inserted += inserted

                # ── Progress reporting ────────────────────────────────────
                if job_id and total_rows_all_sheets > 0:
                    done    = cumulative_rows_done + sheet_inserted
                    percent = min(int(done * 100 / total_rows_all_sheets), 99)
                    progress_store.update_job(
                        job_id,
                        sheet=sheet.sheet_name,
                        rows_done=done,
                        percent=percent,
                        message=(
                            f"Sheet '{sheet.sheet_name}' — "
                            f"batch {batch_num}/{total_batches} "
                            f"({sheet_inserted}/{total_rows} rows)"
                        ),
                    )

                logger.info(
                    "Sheet '%s' batch %s/%s: %s rows committed "
                    "(running total: %s / %s).",
                    sheet.sheet_name,
                    batch_num,
                    total_batches,
                    inserted,
                    sheet_inserted,
                    total_rows,
                )

            except Exception:
                self._db.rollback()
                if job_id:
                    progress_store.update_job(
                        job_id,
                        status="failed",
                        message=f"Error on sheet '{sheet.sheet_name}' batch {batch_num}.",
                    )
                logger.exception(
                    "Sheet '%s' (id=%s) batch %s/%s failed — "
                    "%s rows already committed before this batch.",
                    sheet.sheet_name,
                    sheet.id,
                    batch_num,
                    total_batches,
                    sheet_inserted,
                )
                raise

        logger.info(
            "Sheet '%s' (id=%s): %s / %s row(s) inserted successfully.",
            sheet.sheet_name,
            sheet.id,
            sheet_inserted,
            total_rows,
        )
        return sheet_inserted
