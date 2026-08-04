"""
UploadedSheet Service
---------------------
Orchestrates reading sheet metadata from an Excel workbook and persisting
one :class:`~app.models.uploaded_sheet.UploadedSheet` record per worksheet.
"""

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.uploaded_sheet import UploadedSheet
from app.repository.uploaded_sheet_repository import UploadedSheetRepository
from app.service.File_reader import FileReaderService

logger = logging.getLogger(__name__)


class UploadedSheetService:
    """
    Business-logic layer for sheet ingestion.

    Responsibilities
    ----------------
    - Read every worksheet from the workbook via :class:`FileReaderService`.
    - Derive ``total_rows`` and ``total_columns`` for each sheet.
    - Persist one ``UploadedSheet`` record per worksheet in a single
      database transaction (all-or-nothing).
    """

    def __init__(self, db: Session) -> None:
        self._db = db
        self._repo = UploadedSheetRepository(db)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def ingest_sheets(
        self,
        uploaded_file_id: int,
        file_path: str,
    ) -> list[UploadedSheet]:
        """
        Read the Excel workbook at *file_path*, derive sheet metadata, and
        persist one :class:`UploadedSheet` record for every worksheet.

        All inserts are flushed inside a single ``commit`` so the operation
        is atomic — if any sheet fails to write, no sheets are persisted.

        Parameters
        ----------
        uploaded_file_id:
            PK of the :class:`~app.models.uploaded_file.UploadedFile` record
            that owns these sheets.
        file_path:
            Absolute or relative path to the Excel file on disk.

        Returns
        -------
        list[UploadedSheet]
            Freshly-committed sheet records, ordered by ``sheet_index``.

        Raises
        ------
        FileNotFoundError
            If *file_path* does not exist on disk.
        ValueError
            If the workbook contains no sheets.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(
                f"Cannot ingest sheets: file not found at '{file_path}'."
            )

        logger.info(
            "Ingesting sheets for uploaded_file_id=%s from '%s'.",
            uploaded_file_id,
            file_path,
        )

        # Read workbook metadata via the existing reader service
        workbook_data = FileReaderService.read_excel(path)
        sheets_data: list[dict] = workbook_data.get("sheets", [])

        if not sheets_data:
            raise ValueError(
                f"The workbook '{path.name}' contains no readable sheets."
            )

        created_sheets: list[UploadedSheet] = []

        try:
            for index, sheet_info in enumerate(sheets_data):
                sheet_name: str = sheet_info["name"]

                # "rows" is the integer max_row reported by openpyxl
                total_rows: int = sheet_info.get("rows") or 0

                # "columns" is a list of header strings; count gives total cols
                columns_list: list = sheet_info.get("columns") or []
                total_columns: int = len(columns_list)

                logger.debug(
                    "  Sheet[%s] '%s' — rows=%s, columns=%s",
                    index,
                    sheet_name,
                    total_rows,
                    total_columns,
                )

                sheet_record = self._repo.create(
                    uploaded_file_id=uploaded_file_id,
                    sheet_name=sheet_name,
                    sheet_index=index,
                    total_rows=total_rows,
                    total_columns=total_columns,
                )
                created_sheets.append(sheet_record)

            # Commit every flush in one atomic transaction
            self._db.commit()

            # Refresh all records so their auto-generated fields are populated
            for sheet in created_sheets:
                self._db.refresh(sheet)

        except Exception:
            self._db.rollback()
            logger.exception(
                "Failed to ingest sheets for uploaded_file_id=%s. "
                "Transaction rolled back.",
                uploaded_file_id,
            )
            raise

        logger.info(
            "Ingested %s sheet(s) for uploaded_file_id=%s.",
            len(created_sheets),
            uploaded_file_id,
        )
        return created_sheets
