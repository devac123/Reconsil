"""
Transaction Repository
----------------------
All database interactions for the ``transactions`` table.

``bulk_create`` uses a SQLAlchemy Core INSERT statement in 500-row chunks,
bypassing ORM overhead — the same proven approach as StagingRecordRepository.
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.models.transaction import ProcessingStatus, Transaction

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 500


class TransactionRepository:
    """Data-access layer for :class:`~app.models.transaction.Transaction`."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------ #
    # Write                                                                #
    # ------------------------------------------------------------------ #

    def create(
        self,
        uploaded_file_id: int,
        organization_id: int,
        sheet_name: str,
        data: dict,
        processing_status: ProcessingStatus = ProcessingStatus.PENDING,
    ) -> Transaction:
        """Persist a single transaction record and return the ORM object."""
        now = datetime.utcnow()
        record = Transaction(
            uploaded_file_id=uploaded_file_id,
            organization_id=organization_id,
            sheet_name=sheet_name,
            data=data,
            processing_status=processing_status,
            created_at=now,
            updated_at=now,
        )
        self._db.add(record)
        self._db.flush()
        return record

    def bulk_create(
        self,
        uploaded_file_id: int,
        organization_id: int,
        sheet_name: str,
        rows: list[dict],
        processing_status: ProcessingStatus = ProcessingStatus.PENDING,
    ) -> int:
        """
        Bulk-insert normalised transaction rows using Core INSERT in chunks.

        Parameters
        ----------
        uploaded_file_id:
            FK applied to every row in this batch.
        organization_id:
            FK applied to every row in this batch.
        sheet_name:
            Sheet name applied to every row in this batch.
        rows:
            List of JSON-safe dicts — each dict is one normalised row
            (system fields only, produced by the service layer).
        processing_status:
            Initial status applied to every row; defaults to ``PENDING``.

        Returns
        -------
        int
            Number of rows actually inserted.
        """
        if not rows:
            return 0

        now = datetime.utcnow()
        mappings = [
            {
                "uploaded_file_id":  uploaded_file_id,
                "organization_id":   organization_id,
                "sheet_name":        sheet_name,
                "data":              row,
                "processing_status": processing_status,
                "created_at":        now,
                "updated_at":        now,
            }
            for row in rows
        ]

        stmt = insert(Transaction)
        total_inserted = 0

        for chunk_start in range(0, len(mappings), _CHUNK_SIZE):
            chunk = mappings[chunk_start : chunk_start + _CHUNK_SIZE]
            self._db.execute(stmt, chunk)
            total_inserted += len(chunk)
            logger.debug(
                "  Transactions inserted: rows %s–%s (file_id=%s, sheet='%s').",
                chunk_start + 1,
                chunk_start + len(chunk),
                uploaded_file_id,
                sheet_name,
            )

        return total_inserted

    # ------------------------------------------------------------------ #
    # Read                                                                 #
    # ------------------------------------------------------------------ #

    def get_by_uploaded_file(
        self,
        uploaded_file_id: int,
        skip: int = 0,
        limit: int = 100,
    ) -> list[Transaction]:
        """
        Return transaction records for *uploaded_file_id*, ordered by id.

        Parameters
        ----------
        uploaded_file_id:
            FK of the parent uploaded file.
        skip:
            Number of records to skip (offset).
        limit:
            Maximum number of records to return.
        """
        return (
            self._db.query(Transaction)
            .filter(Transaction.uploaded_file_id == uploaded_file_id)
            .order_by(Transaction.id.asc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_by_uploaded_file(self, uploaded_file_id: int) -> int:
        """Return the total number of transactions for *uploaded_file_id*."""
        return (
            self._db.query(Transaction)
            .filter(Transaction.uploaded_file_id == uploaded_file_id)
            .count()
        )
