"""
FileMapping Service
-------------------
Business logic for creating, updating, and querying column mappings.

Design decisions
~~~~~~~~~~~~~~~~
- ``save_mappings`` performs an **upsert** per entry: it attempts ``create``
  first; if the unique constraint would fire (duplicate natural key) it falls
  back to ``update`` instead.  This means clients can re-POST the same payload
  to change their mind about a mapping without a separate PATCH endpoint.

- ``get_columns_from_staging`` discovers unique column names from the staging
  table by fetching one sample row per distinct ``uploaded_sheet_id`` and
  extracting the JSON keys in Python.  This is portable across databases
  (MySQL, PostgreSQL, SQLite) without resorting to DB-specific JSON functions.
"""

import logging
from collections import defaultdict

from sqlalchemy.orm import Session

from app.models.file_mapping import FileMapping
from app.models.staging_record import StagingRecord
from app.models.uploaded_sheet import UploadedSheet
from app.repository.file_mapping_repository import FileMappingRepository
from app.schemas.file_mapping import MappingEntryCreate

logger = logging.getLogger(__name__)


class FileMappingService:
    """Business-logic layer for :class:`~app.models.file_mapping.FileMapping`."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._repo = FileMappingRepository(db)

    # ------------------------------------------------------------------ #
    # Write                                                                #
    # ------------------------------------------------------------------ #

    def save_mappings(
        self,
        organization_id: int,
        entries: list[MappingEntryCreate],
    ) -> list[FileMapping]:
        """
        Persist all mapping entries for *organization_id*.

        For each entry the method tries to **create** a new record first.
        If a record already exists for the same
        ``(organization_id, sheet_name, excel_column)`` triplet it falls back
        to **update** so that callers can safely re-submit without errors.

        All writes are batched into a single transaction; a rollback fires if
        any entry fails.

        Parameters
        ----------
        organization_id:
            Owner of these mappings.
        entries:
            List of validated :class:`MappingEntryCreate` objects from the
            request body.

        Returns
        -------
        list[FileMapping]
            The saved (created or updated) ORM records.
        """
        logger.info(
            "Saving %s mapping(s) for org_id=%s.", len(entries), organization_id
        )

        saved: list[FileMapping] = []

        try:
            for entry in entries:
                # Check if a record already exists for this natural key
                existing = self._repo.get_mapping(
                    organization_id=organization_id,
                    sheet_name=entry.sheet_name,
                    excel_column=entry.excel_column,
                )

                if existing:
                    # Update in-place
                    record = self._repo.update(
                        organization_id=organization_id,
                        sheet_name=entry.sheet_name,
                        excel_column=entry.excel_column,
                        system_field=entry.system_field,
                        data_type=entry.data_type,
                        is_required=entry.is_required,
                    )
                    logger.debug(
                        "  Updated mapping id=%s ('%s' -> '%s').",
                        record.id,
                        entry.excel_column,
                        entry.system_field,
                    )
                else:
                    # Create a new record
                    record = self._repo.create(
                        organization_id=organization_id,
                        sheet_name=entry.sheet_name,
                        excel_column=entry.excel_column,
                        system_field=entry.system_field,
                        data_type=entry.data_type,
                        is_required=entry.is_required,
                    )
                    logger.debug(
                        "  Created mapping ('%s' -> '%s').",
                        entry.excel_column,
                        entry.system_field,
                    )

                saved.append(record)

            self._db.commit()

            for record in saved:
                self._db.refresh(record)

        except Exception:
            self._db.rollback()
            logger.exception(
                "Failed to save mappings for org_id=%s. Transaction rolled back.",
                organization_id,
            )
            raise

        logger.info(
            "Saved %s mapping(s) for org_id=%s.", len(saved), organization_id
        )
        return saved

    # ------------------------------------------------------------------ #
    # Read                                                                 #
    # ------------------------------------------------------------------ #

    def get_mappings_for_organization(
        self, organization_id: int
    ) -> list[FileMapping]:
        """Return every mapping that belongs to *organization_id*."""
        return self._repo.get_by_organization(organization_id)

    def get_mapping_dict(
        self,
        organization_id: int,
        sheet_name: str | None = None,
    ) -> dict[str, dict[str, str]]:
        """
        Return mappings as a nested dict grouped by sheet name.

        Structure::

            {
                "Revenue": {
                    "Booking ID":  "booking_id",
                    "PNR":         "pnr",
                    "Travel Date": "travel_date",
                },
                "Cost": { … },
            }

        Parameters
        ----------
        organization_id:
            Owner of the mappings.
        sheet_name:
            Optional filter — if provided only that sheet's mappings are
            included.
        """
        if sheet_name:
            records = self._repo.get_by_sheet(organization_id, sheet_name)
        else:
            records = self._repo.get_by_organization(organization_id)

        result: dict[str, dict[str, str]] = defaultdict(dict)
        for r in records:
            result[r.sheet_name][r.excel_column] = r.system_field

        return dict(result)

    def get_columns_from_staging(
        self,
        organization_id: int,
        sheet_name: str,
    ) -> list[str]:
        """
        Discover unique column names for *organization_id* / *sheet_name* by
        inspecting the ``raw_data`` JSON keys in the staging table.

        Strategy
        --------
        1. Find all ``UploadedSheet`` records that match the org + sheet name
           (via their parent ``UploadedFile``).
        2. For each matching sheet, fetch one sample ``StagingRecord``.
        3. Collect all JSON keys across samples and return the unique set,
           sorted alphabetically.

        This approach is portable across MySQL, PostgreSQL, and SQLite because
        it extracts the keys in Python rather than using DB-specific JSON
        functions.
        """
        from app.models.uploaded_file import UploadedFile  # local import avoids circular

        logger.info(
            "Discovering staging columns for org_id=%s, sheet='%s'.",
            organization_id,
            sheet_name,
        )

        # Find uploaded sheets that belong to this org and match the sheet name
        sheet_ids: list[int] = [
            row.id
            for row in (
                self._db.query(UploadedSheet.id)
                .join(UploadedFile, UploadedFile.id == UploadedSheet.uploaded_file_id)
                .filter(
                    UploadedFile.organization_id == organization_id,
                    UploadedSheet.sheet_name == sheet_name,
                )
                .all()
            )
        ]

        if not sheet_ids:
            logger.warning(
                "No uploaded sheets found for org_id=%s, sheet='%s'.",
                organization_id,
                sheet_name,
            )
            return []

        # Fetch one sample row per sheet and collect JSON keys
        all_columns: set[str] = set()
        for sheet_id in sheet_ids:
            sample: StagingRecord | None = (
                self._db.query(StagingRecord)
                .filter(StagingRecord.uploaded_sheet_id == sheet_id)
                .first()
            )
            if sample and isinstance(sample.raw_data, dict):
                all_columns.update(sample.raw_data.keys())

        columns = sorted(all_columns)
        logger.info(
            "Discovered %s column(s) for org_id=%s, sheet='%s'.",
            len(columns),
            organization_id,
            sheet_name,
        )
        return columns
