"""
UploadedFile Repository
-----------------------
All database interactions for the ``uploaded_files`` table live here.
Business logic belongs in the service layer, not here.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.uploaded_file import UploadStatus, UploadedFile

logger = logging.getLogger(__name__)


class UploadedFileRepository:
    """Data-access layer for :class:`~app.models.uploaded_file.UploadedFile`."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------ #
    # Write                                                                #
    # ------------------------------------------------------------------ #

    def create(
        self,
        organization_id: int,
        original_filename: str,
        stored_filename: str,
        file_path: str,
        file_size: int,
        file_extension: str,
        upload_status: UploadStatus = UploadStatus.UPLOADED,
        batch_id: int | None = None,
    ) -> UploadedFile:
        """
        Persist a new :class:`UploadedFile` record and return it.

        Parameters
        ----------
        organization_id:
            FK to the owning ``organizations`` row.
        original_filename:
            The filename as submitted by the client.
        stored_filename:
            The filename actually written to disk (may be sanitised).
        file_path:
            Relative or absolute path to the file on the filesystem.
        file_size:
            Size of the file in bytes.
        file_extension:
            Lower-cased extension including the leading dot, e.g. ``".xlsx"``.
        upload_status:
            Initial lifecycle status; defaults to ``UPLOADED``.
        """
        record = UploadedFile(
            organization_id=organization_id,
            original_filename=original_filename,
            stored_filename=stored_filename,
            file_path=file_path,
            file_size=file_size,
            file_extension=file_extension,
            upload_status=upload_status,
            batch_id=batch_id,
        )
        self._db.add(record)
        self._db.commit()
        self._db.refresh(record)

        logger.info(
            "UploadedFile record created (id=%s, filename='%s').",
            record.id,
            record.original_filename,
        )
        return record

    # ------------------------------------------------------------------ #
    # Read                                                                 #
    # ------------------------------------------------------------------ #

    def get_by_id(self, uploaded_file_id: int) -> Optional[UploadedFile]:
        """Return the :class:`UploadedFile` with the given *id*, or ``None``."""
        return (
            self._db.query(UploadedFile)
            .filter(UploadedFile.id == uploaded_file_id)
            .first()
        )

    def get_all(
        self,
        skip: int = 0,
        limit: int = 100,
    ) -> list[UploadedFile]:
        """
        Return a paginated list of all uploaded-file records, ordered by
        most-recently uploaded first.

        Parameters
        ----------
        skip:
            Number of records to skip (offset).
        limit:
            Maximum number of records to return.
        """
        return (
            self._db.query(UploadedFile)
            .order_by(UploadedFile.uploaded_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )
