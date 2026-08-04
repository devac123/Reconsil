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
- Bulk inserts are done in 500-row chunks via the Core INSERT path in the
  repository layer to keep memory pressure and round-trip count manageable.
"""

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
        # Delegate to Python's built-in scalar conversion
        native = getattr(value, "item", lambda: value)()
        return native

    return value


def _row_to_dict(columns: list[str], values) -> dict:
    """Convert a single pandas row (index, Series or tuple) to a JSON-safe dict."""
    return {col: _make_json_safe(val) for col, val in zip(columns, values)}


class StagingRecordService:
    """
    Business-logic layer for staging-record ingestion.

    For every :class:`~app.models.uploaded_sheet.UploadedSheet` provided,
    this service:

    1. Reads the corresponding worksheet into a DataFrame.
    2. Converts each row to a JSON-safe dict.
    3. Assigns a 1-based ``row_number`` that mirrors the Excel row number
       (header = row 1, first data row = row 2).
    4. Bulk-inserts all rows via the repository layer.
    5. Returns the total row count ingested across all sheets.
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

        # Build row dicts.
        # df.index is the pandas RangeIndex (0-based after dropna reset).
        # Excel row number = pandas index + 2  (row 1 = header, row 2 = first data row).
        # We use the original iloc position before any index reset to keep
        # the mapping honest even when some rows were dropped by dropna.
        rows: list[dict] = []
        for excel_offset, (_, row_series) in enumerate(df.iterrows()):
            # +2: row 1 is the header, data starts at row 2
            row_number = excel_offset + 2
            raw_data = _row_to_dict(columns, row_series)
            rows.append({
                "row_number": row_number,
                "pnr": raw_data.get("PNR"),
                "ticket_number": raw_data.get("Ticket No"),
                "transaction_date": raw_data.get("Date"),
                "raw_data": raw_data,
            })

        inserted = self._repo.bulk_create(
            uploaded_sheet_id=sheet.id,
            rows=rows,
        )

        logger.info(
            "Sheet '%s' (id=%s): %s row(s) queued for insert.",
            sheet.sheet_name,
            sheet.id,
            inserted,
        )
        return inserted
