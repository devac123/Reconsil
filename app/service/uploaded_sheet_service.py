"""
UploadedSheet Service
---------------------
Orchestrates reading sheet metadata from an Excel workbook and persisting
one :class:`~app.models.uploaded_sheet.UploadedSheet` record per worksheet.
"""

import logging
from pathlib import Path
import re

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.uploaded_sheet import UploadedSheet
from app.repository.uploaded_sheet_repository import UploadedSheetRepository
from app.service.File_reader import FileReaderService

logger = logging.getLogger(__name__)

_MYSQL_LOST_CONNECTION_ERRORS = {2006, 2013}


def _normalise_sheet_name(sheet_name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", sheet_name.strip().lower())


def _should_skip_sheet(sheet_name: str) -> bool:
    normalised = _normalise_sheet_name(sheet_name)
    return any(
        marker in normalised
        for marker in (
            "recon",
            "reconsil",
            "reconsilation",
            "reconciliation",
            "reconcilation",
        )
    )


def _is_lost_mysql_connection(exc: OperationalError) -> bool:
    orig = getattr(exc, "orig", None)
    code = None
    if getattr(orig, "args", None):
        code = orig.args[0]
    return bool(
        getattr(exc, "connection_invalidated", False)
        or code in _MYSQL_LOST_CONNECTION_ERRORS
    )


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

        sheet_metadata: list[dict] = []
        for index, sheet_info in enumerate(sheets_data):
            sheet_name: str = sheet_info["name"]
            if _should_skip_sheet(sheet_name):
                logger.info(
                    "Skipping reconciliation/result sheet '%s' for uploaded_file_id=%s.",
                    sheet_name,
                    uploaded_file_id,
                )
                continue

            header_row = int(sheet_info.get("header_row") or 0)
            total_rows = max(int(sheet_info.get("rows") or 0) - header_row - 1, 0)
            total_columns = len([
                column
                for column in sheet_info.get("columns", [])
                if str(column).strip() and not str(column).startswith("Unnamed:")
            ])

            logger.debug(
                "  Sheet[%s] '%s' — rows=%s, columns=%s",
                index,
                sheet_name,
                total_rows,
                total_columns,
            )
            sheet_metadata.append(
                {
                    "sheet_name": sheet_name,
                    "sheet_index": index,
                    "total_rows": total_rows,
                    "total_columns": total_columns,
                    "header_row": header_row,
                }
            )

        if not sheet_metadata:
            raise ValueError(
                f"The workbook '{path.name}' contains no processable sheets."
            )

        created_sheets: list[UploadedSheet] = []
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            created_sheets = []
            try:
                for metadata in sheet_metadata:
                    create_payload = {
                        key: value
                        for key, value in metadata.items()
                        if key != "header_row"
                    }
                    sheet_record = self._repo.create(
                        uploaded_file_id=uploaded_file_id,
                        **create_payload,
                    )
                    sheet_record.detected_header_row = metadata.get("header_row")
                    created_sheets.append(sheet_record)

                # Commit every flush in one atomic transaction
                self._db.commit()

                # Refresh all records so their auto-generated fields are populated
                for sheet in created_sheets:
                    self._db.refresh(sheet)
                    matching_metadata = next(
                        (
                            item
                            for item in sheet_metadata
                            if item["sheet_index"] == sheet.sheet_index
                        ),
                        None,
                    )
                    if matching_metadata:
                        sheet.detected_header_row = matching_metadata.get("header_row")
                break

            except OperationalError as exc:
                self._db.rollback()
                if attempt < max_attempts and _is_lost_mysql_connection(exc):
                    logger.warning(
                        "Lost MySQL connection while ingesting sheets for "
                        "uploaded_file_id=%s; retrying metadata insert once.",
                        uploaded_file_id,
                    )
                    continue
                logger.exception(
                    "Failed to ingest sheets for uploaded_file_id=%s. "
                    "Transaction rolled back.",
                    uploaded_file_id,
                )
                raise

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
