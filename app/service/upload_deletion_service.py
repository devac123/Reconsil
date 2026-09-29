"""
Uploaded file deletion service.

Deletes an uploaded workbook and the imported/reconciled data derived from it.
The database work is intentionally set-based so large Excel imports do not
require loading thousands of child IDs into Python before deleting them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.reconciliation_remark import ReconciliationRemark
from app.models.reconciliation_result import ReconciliationResult
from app.models.staging_record import StagingRecord
from app.models.upload_batch import UploadBatch
from app.models.uploaded_file import UploadedFile
from app.models.uploaded_sheet import UploadedSheet

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str], None]


class UploadedFileDeleteNotFoundError(ValueError):
    """Raised when an uploaded file delete target no longer exists."""


class UploadedFileDeletionService:
    """Business logic for deleting uploaded files and their dependent data."""

    def __init__(self, db: Session, upload_dir: Path = Path("file")) -> None:
        self._db = db
        self._upload_dir = upload_dir

    def delete(
        self,
        uploaded_file_id: int,
        delete_batch: bool = False,
        progress: ProgressCallback | None = None,
    ) -> dict:
        """
        Delete one upload or, when requested, the whole upload batch.

        Returns a summary dict suitable for API/job responses.
        """
        self._emit(progress, 5, "Preparing delete.")
        target = (
            self._db.query(UploadedFile.id, UploadedFile.batch_id)
            .filter(UploadedFile.id == uploaded_file_id)
            .first()
        )
        if not target:
            raise UploadedFileDeleteNotFoundError("Uploaded file not found.")

        batch_id = target.batch_id if delete_batch else None
        file_rows_query = self._db.query(UploadedFile.id, UploadedFile.file_path)
        if batch_id:
            file_rows_query = file_rows_query.filter(UploadedFile.batch_id == batch_id)
        else:
            file_rows_query = file_rows_query.filter(UploadedFile.id == uploaded_file_id)

        file_rows = file_rows_query.all()
        file_ids = [row.id for row in file_rows]
        stored_paths = [row.file_path for row in file_rows if row.file_path]
        if not file_ids:
            raise UploadedFileDeleteNotFoundError("Uploaded file not found.")

        try:
            self._emit(progress, 15, "Deleting")
            summary = self._delete_database_rows(file_ids)

            self._emit(progress, 85, "Deleting")
            deleted_batch = False
            if batch_id:
                deleted_batch = (
                    self._db.query(UploadBatch)
                    .filter(UploadBatch.id == batch_id)
                    .delete(synchronize_session=False)
                ) > 0
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

        self._emit(progress, 95, "Removing stored workbook files.")
        removed_from_disk, disk_delete_failures, skipped_paths = (
            self._delete_stored_files(stored_paths)
        )

        self._emit(progress, 100, "Delete complete.")
        return {
            "status": "deleted",
            "uploaded_file_id": uploaded_file_id,
            "batch_id": batch_id,
            "deleted_batch": deleted_batch,
            "removed_from_disk": removed_from_disk,
            "disk_delete_failures": disk_delete_failures,
            "skipped_paths": skipped_paths,
            **summary,
        }

    def _delete_database_rows(self, file_ids: list[int]) -> dict:
        sheet_ids = (
            select(UploadedSheet.id)
            .where(UploadedSheet.uploaded_file_id.in_(file_ids))
        )
        result_ids = (
            select(ReconciliationResult.id)
            .where(ReconciliationResult.uploaded_file_id.in_(file_ids))
        )

        deleted_remarks = (
            self._db.query(ReconciliationRemark)
            .filter(ReconciliationRemark.result_id.in_(result_ids))
            .delete(synchronize_session=False)
        )

        deleted_results = (
            self._db.query(ReconciliationResult)
            .filter(ReconciliationResult.uploaded_file_id.in_(file_ids))
            .delete(synchronize_session=False)
        )

        deleted_staging = (
            self._db.query(StagingRecord)
            .filter(StagingRecord.uploaded_file_id.in_(file_ids))
            .delete(synchronize_session=False)
        )
        legacy_staging = (
            self._db.query(StagingRecord)
            .filter(
                StagingRecord.uploaded_file_id.is_(None),
                StagingRecord.uploaded_sheet_id.in_(sheet_ids),
            )
            .delete(synchronize_session=False)
        )
        deleted_staging += legacy_staging

        deleted_sheets = (
            self._db.query(UploadedSheet)
            .filter(UploadedSheet.uploaded_file_id.in_(file_ids))
            .delete(synchronize_session=False)
        )

        deleted_files = (
            self._db.query(UploadedFile)
            .filter(UploadedFile.id.in_(file_ids))
            .delete(synchronize_session=False)
        )

        return {
            "deleted_files": deleted_files,
            "deleted_sheets": deleted_sheets,
            "deleted_staging_records": deleted_staging,
            "deleted_results": deleted_results,
            "deleted_remarks": deleted_remarks,
        }

    def _delete_stored_files(self, stored_paths: list[str]) -> tuple[int, int, list[str]]:
        removed = 0
        failed = 0
        skipped: list[str] = []

        for stored_path in stored_paths:
            safe_path = self._safe_upload_path(stored_path)
            if safe_path is None:
                skipped.append(stored_path)
                continue

            try:
                if safe_path.exists() and safe_path.is_file():
                    safe_path.unlink()
                    removed += 1
            except OSError:
                failed += 1
                logger.warning("Could not remove stored upload file '%s'.", safe_path, exc_info=True)

        return removed, failed, skipped

    def _safe_upload_path(self, stored_path: str) -> Path | None:
        upload_root = self._upload_dir
        if not upload_root.is_absolute():
            upload_root = Path.cwd() / upload_root
        upload_root = upload_root.resolve()

        candidate = Path(stored_path)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(upload_root)
        except (OSError, ValueError):
            logger.warning("Skipping stored upload path outside upload directory: '%s'.", stored_path)
            return None
        return resolved

    @staticmethod
    def _emit(progress: ProgressCallback | None, percent: int, message: str) -> None:
        if progress:
            progress(percent, message)
