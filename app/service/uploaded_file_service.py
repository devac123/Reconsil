"""
UploadedFile Service
--------------------
Orchestrates the creation and retrieval of uploaded-file metadata records.
All callers (e.g. route handlers) should go through this service rather than
touching the repository directly.
"""

import logging
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.models.uploaded_file import UploadStatus, UploadedFile
from app.repository.uploaded_file_repository import UploadedFileRepository

logger = logging.getLogger(__name__)


class UploadedFileService:
    """Business-logic layer for :class:`~app.models.uploaded_file.UploadedFile`."""

    def __init__(self, db: Session) -> None:
        self._repo = UploadedFileRepository(db)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def record_upload(
        self,
        organization_id: int,
        original_filename: str,
        stored_path: str,
    ) -> UploadedFile:
        """
        Create and persist a metadata record for a freshly-uploaded file.

        This method derives ``stored_filename``, ``file_size``,
        ``file_extension``, and ``file_path`` from *stored_path* so that
        callers only need to pass the values they already have in hand.

        Parameters
        ----------
        organization_id:
            ID of the :class:`~app.models.organization.Organization` that
            owns this file (resolved by the organisation service beforehand).
        original_filename:
            The filename as submitted by the HTTP client
            (``UploadFile.filename``).
        stored_path:
            The path where the file was written to disk.

        Returns
        -------
        UploadedFile
            The freshly-created ORM record (already committed).

        Raises
        ------
        FileNotFoundError
            If *stored_path* does not point to an existing file.
        """
        path = Path(stored_path)

        if not path.exists():
            raise FileNotFoundError(
                f"Cannot record upload: file not found at '{stored_path}'."
            )

        file_size = path.stat().st_size
        file_extension = path.suffix.lower()   # e.g. ".xlsx"
        stored_filename = path.name            # just the filename component

        logger.info(
            "Recording upload — org_id=%s, original='%s', size=%s bytes.",
            organization_id,
            original_filename,
            file_size,
        )

        uploaded_file = self._repo.create(
            organization_id=organization_id,
            original_filename=original_filename,
            stored_filename=stored_filename,
            file_path=str(path),
            file_size=file_size,
            file_extension=file_extension,
            upload_status=UploadStatus.UPLOADED,
        )

        return uploaded_file

    def get_by_id(self, uploaded_file_id: int) -> Optional[UploadedFile]:
        """Return the :class:`UploadedFile` with *uploaded_file_id*, or ``None``."""
        return self._repo.get_by_id(uploaded_file_id)

    def get_all(self, skip: int = 0, limit: int = 100) -> list[UploadedFile]:
        """Return a paginated list of uploaded-file records."""
        return self._repo.get_all(skip=skip, limit=limit)
