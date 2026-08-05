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
- Each sheet is processed in BATCH_SIZE-row chunks (default 10,000).
  Each chunk is inserted and committed independently so:
    • Memory stays flat regardless of sheet size.
    • A failure mid-sheet rolls back only the current chunk; already-committed
      chunks are preserved and logged so the operator knows where to resume.
- Per-sheet PNR / ticket / date column names are resolved through
  ``_SHEET_FIELD_MAP`` so the staging table always has populated searchable
  fields regardless of the source sheet's naming conventions.
"""

import logging
import math
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Generator

import pandas as pd

from sqlalchemy.orm import Session

from app.models.uploaded_sheet import UploadedSheet
from app.repository.staging_record_repository import StagingRecordRepository
from app.service.File_reader import FileReaderService

logger = logging.getLogger(__name__)

# How many Excel rows to process and commit per batch.
# 10,000 balances memory use against round-trip count.
BATCH_SIZE = 10_000


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

def _make_json_safe(value: Any) -> Any:
    """Coerce *value* to a DB JSON-serialisable Python type."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        import pandas as _pd
        if value is _pd.NaT:
            return None
        if isinstance(value, _pd.Timestamp):
            return value.isoformat()
    except ImportError:
        pass
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    type_name = type(value).__module__
    if type_name == "numpy":
        return getattr(value, "item", lambda: value)()
    return value


def _row_to_dict(columns: list[str], values) -> dict:
    return {col: _make_json_safe(val) for col, val in zip(columns, values)}


def _to_date_str(value: Any) -> str | None:
    """Coerce *value* to an ISO-8601 date string or ``None``."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        import pandas as _pd
        if value is _pd.NaT:
            return None
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
    # numpy scalars
    type_name = type(value).__module__
    if type_name == "numpy":
        value = getattr(value, "item", lambda: value)()
    # Excel serial integer (days since 1899-12-30)
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


# ---------------------------------------------------------------------------
# DataFrame chunk generator
# ---------------------------------------------------------------------------

def _iter_df_chunks(
    df: pd.DataFrame,
    chunk_size: int,
) -> Generator[pd.DataFrame, None, None]:
    """
    Yield successive *chunk_size*-row slices of *df*.
    Uses iloc so no index assumptions are made.
    """
    total = len(df)
    for start in range(0, total, chunk_size):
        yield df.iloc[start : start + chunk_size]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class StagingRecordService:
    """
    Business-logic layer for staging-record ingestion.

    Processing model
    ----------------
    For each sheet the service:

    1. Reads the full worksheet into a DataFrame once (pandas keeps it as a
       column-store so this is RAM-efficient for wide sheets).
    2. Slices the DataFrame into BATCH_SIZE-row chunks.
    3. For each chunk:
       a. Converts rows to JSON-safe dicts.
       b. Calls the repository bulk_create (500-row DB inserts internally).
       c. Commits immediately so the DB receives data incrementally.
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
            Rows per commit batch.  Defaults to ``BATCH_SIZE`` (10 000).

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

        total_rows_inserted = 0

        for sheet in sheets:
            sheet_rows = self._ingest_single_sheet(path, sheet, batch_size)
            total_rows_inserted += sheet_rows

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

        columns   = df.columns.tolist()
        field_map = _field_map_for(sheet.sheet_name)
        total_rows = len(df)
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
            # Global row offset for row_number: first data row = 1
            row_offset = (batch_num - 1) * batch_size

            rows: list[dict] = []
            for local_idx, (_, row_series) in enumerate(chunk_df.iterrows()):
                row_number = row_offset + local_idx + 1
                raw_data   = _row_to_dict(columns, row_series)
                rows.append({
                    "row_number":       row_number,
                    "pnr":              _extract_field(raw_data, field_map["pnr"]),
                    "ticket_number":    _extract_field(raw_data, field_map["ticket_number"]),
                    "transaction_date": _extract_date_field(raw_data, field_map["transaction_date"]),
                    "raw_data":         raw_data,
                })

            try:
                inserted = self._repo.bulk_create(
                    uploaded_sheet_id=sheet.id,
                    rows=rows,
                )
                self._db.commit()
                sheet_inserted += inserted

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


import logging
import math
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from sqlalchemy.orm import Session

from app.models.uploaded_sheet import UploadedSheet
from app.repository.staging_record_repository import StagingRecordRepository
from app.service.File_reader import FileReaderService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-sheet field-name mapping
# ---------------------------------------------------------------------------
# Maps a normalised sheet name → dict of logical field → actual Excel column.
#
# Logical fields used downstream:
#   pnr              – the booking/PNR reference (6-char code)
#   ticket_number    – ticket/document number where available
#   transaction_date – date of the transaction / booking
#
# Keys are lower-cased + stripped for case-insensitive lookup.

_SHEET_FIELD_MAP: dict[str, dict[str, str]] = {
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
}

_DEFAULT_FIELD_MAP: dict[str, str | None] = {
    "pnr":              None,
    "ticket_number":    None,
    "transaction_date": None,
}


def _field_map_for(sheet_name: str) -> dict[str, str | None]:
    """Return the field-name map for *sheet_name* (case-insensitive lookup)."""
    return _SHEET_FIELD_MAP.get(sheet_name.strip().lower(), _DEFAULT_FIELD_MAP)


# ---------------------------------------------------------------------------
# JSON-safety helpers
# ---------------------------------------------------------------------------

def _make_json_safe(value: Any) -> Any:
    """
    Coerce *value* into a type that the database JSON column can serialise.

    Rules
    -----
    - ``None`` / ``float('nan')`` / ``pd.NaT``  →  ``None``   (stored as JSON null)
    - ``datetime`` / ``date``                    →  ISO-8601 string
    - ``Decimal``                                →  ``float``
    - numpy integer or float scalars             →  Python int / float
    - Everything else                            →  unchanged
    """
    if value is None:
        return None

    # pandas NaT and float NaN both signal "no value"
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        import pandas as _pd
        if value is _pd.NaT:
            return None
        if isinstance(value, _pd.Timestamp):
            return value.isoformat()
    except ImportError:
        pass

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return float(value)

    # numpy scalar types: int64, float64, bool_, etc.
    type_name = type(value).__module__
    if type_name == "numpy":
        native = getattr(value, "item", lambda: value)()
        return native

    return value


def _row_to_dict(columns: list[str], values) -> dict:
    """Convert a single pandas row (index, Series or tuple) to a JSON-safe dict."""
    return {col: _make_json_safe(val) for col, val in zip(columns, values)}


def _to_date_str(value: Any) -> str | None:
    """
    Coerce *value* to an ISO-8601 date string suitable for a SQL DATE column.

    Handles:
    - ``None`` / NaN / NaT              → ``None``
    - ``datetime`` / ``date``           → ``"YYYY-MM-DD"``
    - ``pandas.Timestamp``              → ``"YYYY-MM-DD"``
    - ISO string ``"YYYY-MM-DD..."``    → first 10 chars
    - Excel serial integer (e.g. 44382) → converted via Excel epoch
    - Anything else unrecognisable      → ``None``
    """
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None

    try:
        import pandas as _pd
        if value is _pd.NaT:
            return None
        if isinstance(value, _pd.Timestamp):
            return value.date().isoformat()
    except ImportError:
        pass

    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        # Accept "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS" etc.
        if len(stripped) >= 10 and stripped[4] == "-":
            return stripped[:10]
        return None

    # numpy int/float scalars
    type_name = type(value).__module__
    if type_name == "numpy":
        value = getattr(value, "item", lambda: value)()

    # Excel serial number: days since 1899-12-30 (Lotus leap-year bug included)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            n = int(value)
            if 1 <= n <= 2958465:   # 1900-01-01 .. 9999-12-31 in Excel serials
                from datetime import date as _date, timedelta
                return (_date(1899, 12, 30) + timedelta(days=n)).isoformat()
        except (ValueError, OverflowError):
            pass

    return None


def _extract_field(raw_data: dict, column_name: str | None) -> Any:
    """
    Safely pull a value from *raw_data* by *column_name*.
    Returns ``None`` if *column_name* is None or not present.
    """
    if not column_name:
        return None
    value = raw_data.get(column_name)
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _extract_date_field(raw_data: dict, column_name: str | None) -> str | None:
    """
    Like ``_extract_field`` but always returns an ISO date string or ``None``.
    Converts Excel serial integers, pandas Timestamps, datetime objects, etc.
    Safe to pass directly into a SQL DATE column.
    """
    return _to_date_str(_extract_field(raw_data, column_name))


class StagingRecordService:
    """
    Business-logic layer for staging-record ingestion.

    For every :class:`~app.models.uploaded_sheet.UploadedSheet` provided,
    this service:

    1. Reads the corresponding worksheet into a DataFrame (using the correct
       header row for each sheet via :class:`FileReaderService`).
    2. Converts each row to a JSON-safe dict.
    3. Extracts ``pnr``, ``ticket_number``, and ``transaction_date`` using
       the per-sheet field map so the staging table has populated index
       columns for every source sheet.
    4. Assigns a 1-based ``row_number`` relative to the data rows
       (first data row = 1, regardless of how many header/title rows
       were skipped during read).
    5. Bulk-inserts all rows via the repository layer.
    6. Returns the total row count ingested across all sheets.
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
    ) -> int:
        """
        Read every data row from the workbook and bulk-insert staging records.

        Parameters
        ----------
        file_path:
            Path to the Excel file on disk.
        sheets:
            Committed :class:`UploadedSheet` objects (must already have IDs).

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

        total_rows_inserted = 0

        try:
            for sheet in sheets:
                sheet_rows = self._ingest_single_sheet(path, sheet)
                total_rows_inserted += sheet_rows

            # One commit covers all sheets — atomic across the entire file
            self._db.commit()

        except Exception:
            self._db.rollback()
            logger.exception(
                "Failed to ingest staging rows from '%s'. "
                "Transaction rolled back.",
                file_path,
            )
            raise

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
    ) -> int:
        """
        Read one worksheet and bulk-insert its rows.

        Returns the number of rows inserted for this sheet.
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
                "Sheet '%s' (id=%s) contains no data rows. Skipping.",
                sheet.sheet_name,
                sheet.id,
            )
            return 0

        columns = df.columns.tolist()
        field_map = _field_map_for(sheet.sheet_name)

        rows: list[dict] = []
        for data_offset, (_, row_series) in enumerate(df.iterrows()):
            # row_number is 1-based relative to data rows
            # (title/header rows are already stripped by read_sheet_as_dataframe)
            row_number = data_offset + 1
            raw_data = _row_to_dict(columns, row_series)

            rows.append({
                "row_number":        row_number,
                "pnr":               _extract_field(raw_data, field_map["pnr"]),
                "ticket_number":     _extract_field(raw_data, field_map["ticket_number"]),
                "transaction_date":  _extract_date_field(raw_data, field_map["transaction_date"]),
                "raw_data":          raw_data,
            })

        inserted = self._repo.bulk_create(
            uploaded_sheet_id=sheet.id,
            rows=rows,
        )

        logger.info(
            "Sheet '%s' (id=%s): %s row(s) inserted.",
            sheet.sheet_name,
            sheet.id,
            inserted,
        )
        return inserted
