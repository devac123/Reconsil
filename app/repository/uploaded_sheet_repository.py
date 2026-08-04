"""
UploadedSheet Repository
------------------------
All database interactions for the ``uploaded_sheets`` table.
Business logic belongs in the service layer, not here.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.uploaded_sheet import UploadedSheet

logger = logging.getLogger(__name__)


class UploadedSheetRepository:
    """Data-access layer for :class:`~app.models.uploaded_sheet.UploadedSheet`."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------ #
    # Write                                                                #
    # ------------------------------------------------------------------ #

    def create(
        self,
        uploaded_file_id: int,
        sheet_name: str,
        sheet_index: int,
        total_rows: int,
        total_columns: int,
    ) -> UploadedSheet:
        """
        Persist a single sheet-metadata record and return it.

        Parameters
        ----------
        uploaded_file_id:
            FK to the parent ``uploaded_files`` row.
        sheet_name:
            Name of the worksheet tab (e.g. ``"Revenue"``).
        sheet_index:
            Zero-based position of the sheet in the workbook.
        total_rows:
            Total number of rows reported by openpyxl (includes header row).
        total_columns:
            Number of columns detected from the header row.
        """
        record = UploadedSheet(
            uploaded_file_id=uploaded_file_id,
            sheet_name=sheet_name,
            sheet_index=sheet_index,
            total_rows=total_rows,
            total_columns=total_columns,
        )
        self._db.add(record)
        self._db.flush()   # write to DB within the current transaction

        logger.info(
            "UploadedSheet record queued (file_id=%s, index=%s, name='%s').",
            uploaded_file_id,
            sheet_index,
            sheet_name,
        )
        return record

    # ------------------------------------------------------------------ #
    # Read                                                                 #
    # ------------------------------------------------------------------ #

    def get_by_uploaded_file(
        self,
        uploaded_file_id: int,
    ) -> list[UploadedSheet]:
        """
        Return all sheet records that belong to *uploaded_file_id*, ordered
        by sheet index (ascending).
        """
        return (
            self._db.query(UploadedSheet)
            .filter(UploadedSheet.uploaded_file_id == uploaded_file_id)
            .order_by(UploadedSheet.sheet_index.asc())
            .all()
        )

    def get_by_id(self, sheet_id: int) -> Optional[UploadedSheet]:
        """Return the :class:`UploadedSheet` with *sheet_id*, or ``None``."""
        return (
            self._db.query(UploadedSheet)
            .filter(UploadedSheet.id == sheet_id)
            .first()
        )
