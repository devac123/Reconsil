"""
Transaction Service
-------------------
Transforms raw staging records into normalised transaction records by applying
the organisation's column mappings.

Algorithm
~~~~~~~~~
For each UploadedSheet belonging to the given UploadedFile:

  1. Load the FileMapping for (organization_id, sheet_name) once — builds a
     lookup dict  ``{excel_column: system_field}``.
  2. Skip the sheet entirely if no mappings exist for it.
  3. Stream staging rows in chunks (via the generator on the repository) to
     keep memory usage flat on large files.
  4. For every chunk, apply the mapping to each raw_data dict:
       - For each mapped excel_column present in raw_data, write its value
         under the system_field key in the output dict.
       - Unmapped columns are silently ignored.
       - Rows that produce an empty output dict (no mapped column had a
         matching key) are also skipped.
  5. Bulk-insert the normalised dicts into the transactions table.
  6. Flip is_processed = True on the staging records in the same chunk
     using a single UPDATE statement.
  7. Commit once per chunk so progress is durable; a partial failure loses
     at most one chunk rather than the entire file.

Returns the total number of transaction rows inserted across all sheets.
"""

import logging
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.staging_record import StagingRecord
from app.models.uploaded_sheet import UploadedSheet
from app.repository.file_mapping_repository import FileMappingRepository
from app.repository.staging_record_repository import StagingRecordRepository
from app.repository.transaction_repository import TransactionRepository

logger = logging.getLogger(__name__)


class TransactionService:
    """
    Business-logic layer for staging-to-transaction transformation.
    """

    def __init__(self, db: Session) -> None:
        self._db = db
        self._staging_repo = StagingRecordRepository(db)
        self._txn_repo = TransactionRepository(db)
        self._mapping_repo = FileMappingRepository(db)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def process_file(
        self,
        uploaded_file_id: int,
        organization_id: int,
    ) -> int:
        """
        Transform every unprocessed staging record for *uploaded_file_id*
        into a normalised transaction record.

        Parameters
        ----------
        uploaded_file_id:
            PK of the :class:`~app.models.uploaded_file.UploadedFile` to
            process.
        organization_id:
            PK of the owning organisation — used to look up column mappings.

        Returns
        -------
        int
            Total number of transaction rows written across all sheets.

        Raises
        ------
        ValueError
            If no uploaded sheets are found for *uploaded_file_id*.
        """
        # Fetch all sheets that belong to this file
        sheets: list[UploadedSheet] = (
            self._db.query(UploadedSheet)
            .filter(UploadedSheet.uploaded_file_id == uploaded_file_id)
            .order_by(UploadedSheet.sheet_index.asc())
            .all()
        )

        if not sheets:
            raise ValueError(
                f"No uploaded sheets found for uploaded_file_id={uploaded_file_id}."
            )

        total_inserted = 0

        for sheet in sheets:
            inserted = self._process_sheet(
                uploaded_file_id=uploaded_file_id,
                organization_id=organization_id,
                sheet=sheet,
            )
            total_inserted += inserted

        logger.info(
            "process_file complete: uploaded_file_id=%s — %s transaction(s) created.",
            uploaded_file_id,
            total_inserted,
        )
        return total_inserted

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    def _process_sheet(
        self,
        uploaded_file_id: int,
        organization_id: int,
        sheet: UploadedSheet,
    ) -> int:
        """
        Transform all staging rows for *sheet* and persist transactions.

        Returns the number of transactions inserted for this sheet.
        """
        sheet_name = sheet.sheet_name

        # Build mapping lookup once: { excel_column -> system_field }
        mappings = self._mapping_repo.get_by_sheet(organization_id, sheet_name)
        if not mappings:
            logger.warning(
                "No mappings defined for org_id=%s, sheet='%s'. Skipping.",
                organization_id,
                sheet_name,
            )
            return 0

        column_map: dict[str, str] = {
            m.excel_column: m.system_field for m in mappings
        }

        logger.info(
            "Processing sheet '%s' (id=%s) — %s mapped column(s).",
            sheet_name,
            sheet.id,
            len(column_map),
        )

        total_inserted = 0

        # Stream staging rows chunk by chunk
        for chunk in self._staging_repo.get_all_by_sheet(sheet.id):
            normalised_rows: list[dict] = []
            processed_ids: list[int] = []

            for record in chunk:
                normalised = self._apply_mapping(record.raw_data, column_map)
                if not normalised:
                    # Row had no mapped columns — skip but still mark processed
                    logger.debug(
                        "  Row %s (staging_id=%s) produced no mapped fields; skipping.",
                        record.row_number,
                        record.id,
                    )
                    processed_ids.append(record.id)
                    continue

                normalised_rows.append(normalised)
                processed_ids.append(record.id)

            # Bulk-insert this chunk of normalised rows
            if normalised_rows:
                inserted = self._txn_repo.bulk_create(
                    uploaded_file_id=uploaded_file_id,
                    organization_id=organization_id,
                    sheet_name=sheet_name,
                    rows=normalised_rows,
                )
                total_inserted += inserted

            # Mark staging records in this chunk as processed
            if processed_ids:
                self._db.execute(
                    update(StagingRecord)
                    .where(StagingRecord.id.in_(processed_ids))
                    .values(is_processed=True, updated_at=datetime.utcnow())
                )

            # Commit after each chunk — durable progress, bounded rollback window
            self._db.commit()

            logger.debug(
                "  Chunk committed: %s transaction(s) inserted, "
                "%s staging record(s) marked processed (sheet='%s').",
                len(normalised_rows),
                len(processed_ids),
                sheet_name,
            )

        logger.info(
            "Sheet '%s' (id=%s) done — %s transaction(s) created.",
            sheet_name,
            sheet.id,
            total_inserted,
        )
        return total_inserted

    @staticmethod
    def _apply_mapping(
        raw_data: dict,
        column_map: dict[str, str],
    ) -> dict:
        """
        Translate *raw_data* keys using *column_map*.

        Only excel columns present in *column_map* are included in the
        output.  Columns not in the mapping are silently discarded.

        Parameters
        ----------
        raw_data:
            Original staging row dict ``{excel_column: value}``.
        column_map:
            Lookup table ``{excel_column: system_field}``.

        Returns
        -------
        dict
            Normalised dict ``{system_field: value}``.  Empty dict if no
            mapped columns were found in *raw_data*.
        """
        return {
            system_field: raw_data[excel_col]
            for excel_col, system_field in column_map.items()
            if excel_col in raw_data
        }
