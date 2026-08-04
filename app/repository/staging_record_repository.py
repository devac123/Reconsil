"""
StagingRecord Repository
------------------------
All database interactions for the ``staging_records`` table.
The bulk_create method uses a SQLAlchemy Core INSERT statement so the ORM
object-construction overhead is bypassed entirely — critical for 10k+ rows.
"""

import logging
from datetime import datetime

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.models.staging_record import StagingRecord

logger = logging.getLogger(__name__)

# How many rows to send to the database per round-trip.
# Keeps memory pressure low and avoids enormous single SQL statements.
_CHUNK_SIZE = 500


class StagingRecordRepository:
    """Data-access layer for :class:`~app.models.staging_record.StagingRecord`."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------ #
    # Write                                                                #
    # ------------------------------------------------------------------ #

    def create(
        self,
        uploaded_sheet_id: int,
        row_number: int,
        raw_data: dict,
    ) -> StagingRecord:
        """
        Persist a single staging record and return the ORM object.

        Prefer :meth:`bulk_create` when inserting more than a handful of rows.
        """
        now = datetime.utcnow()
        record = StagingRecord(
            uploaded_sheet_id=uploaded_sheet_id,
            row_number=row_number,
            raw_data=raw_data,
            is_processed=False,
            created_at=now,
            updated_at=now,
        )
        self._db.add(record)
        self._db.flush()
        return record

    def bulk_create(
        self,
        uploaded_sheet_id: int,
        rows: list[dict],
    ) -> int:
        """
        Insert *rows* into ``staging_records`` in chunks using a Core INSERT
        statement. Returns the total number of rows inserted.

        Parameters
        ----------
        uploaded_sheet_id:
            FK value applied to every row in this batch.
        rows:
            List of dicts ``{"row_number": int, "raw_data": dict}``.
            The service layer is responsible for building this list.

        Returns
        -------
        int
            Number of rows actually inserted.
        """
        if not rows:
            return 0

        now = datetime.utcnow()

        # Build flat list of insert mappings
        mappings = [
            {
                "uploaded_sheet_id": uploaded_sheet_id,
                "row_number": row["row_number"],
                "raw_data": row["raw_data"],
                "is_processed": False,
                "created_at": now,
                "updated_at": now,
            }
            for row in rows
        ]

        total_inserted = 0
        stmt = insert(StagingRecord)

        for chunk_start in range(0, len(mappings), _CHUNK_SIZE):
            chunk = mappings[chunk_start : chunk_start + _CHUNK_SIZE]
            self._db.execute(stmt, chunk)
            total_inserted += len(chunk)
            logger.debug(
                "  Inserted chunk rows %s–%s (sheet_id=%s).",
                chunk_start + 1,
                chunk_start + len(chunk),
                uploaded_sheet_id,
            )

        return total_inserted

    # ------------------------------------------------------------------ #
    # Read                                                                 #
    # ------------------------------------------------------------------ #

    def get_by_sheet(
        self,
        uploaded_sheet_id: int,
        skip: int = 0,
        limit: int = 100,
    ) -> list[StagingRecord]:
        """
        Return a paginated slice of staging records for *uploaded_sheet_id*,
        ordered by row number.

        Parameters
        ----------
        uploaded_sheet_id:
            FK of the parent sheet.
        skip:
            Number of records to skip (offset).
        limit:
            Maximum number of records to return.
        """
        return (
            self._db.query(StagingRecord)
            .filter(StagingRecord.uploaded_sheet_id == uploaded_sheet_id)
            .order_by(StagingRecord.row_number.asc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_all_by_sheet(
        self,
        uploaded_sheet_id: int,
        chunk_size: int = _CHUNK_SIZE,
    ):
        """
        Yield every staging record for *uploaded_sheet_id* in fixed-size
        chunks, ordered by row number.

        Using a generator rather than loading all rows at once keeps memory
        consumption flat for large sheets (tens of thousands of rows).

        Parameters
        ----------
        uploaded_sheet_id:
            FK of the parent sheet.
        chunk_size:
            Number of rows fetched per database round-trip.

        Yields
        ------
        list[StagingRecord]
            A list of up to *chunk_size* records per iteration.
        """
        offset = 0
        while True:
            chunk: list[StagingRecord] = (
                self._db.query(StagingRecord)
                .filter(StagingRecord.uploaded_sheet_id == uploaded_sheet_id)
                .order_by(StagingRecord.row_number.asc())
                .offset(offset)
                .limit(chunk_size)
                .all()
            )
            if not chunk:
                break
            yield chunk
            if len(chunk) < chunk_size:
                # Last partial chunk — no more rows to fetch
                break
            offset += chunk_size
