"""
File Upload Routes
------------------
Handles HTTP concerns only. All business logic is delegated to the service
layer.

Flow
~~~~
1. Save the uploaded file to disk.
2. Detect / create the Organization from the filename.
3. Record the upload in ``uploaded_files`` (status = UPLOADED).
4. Ingest sheet metadata into ``uploaded_sheets``.
5. Bulk-ingest all data rows into ``staging_records``
   (status transitions: PROCESSING → PROCESSED / FAILED).
6. Return a structured JSON response.
"""

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.uploaded_file import UploadStatus
from app.service.organization_service import get_or_create_organization
from app.service.staging_record_service import StagingRecordService
from app.service.uploaded_file_service import UploadedFileService
from app.service.uploaded_sheet_service import UploadedSheetService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/files",
    tags=["Files"],
)

UPLOAD_DIR = Path("file")
UPLOAD_DIR.mkdir(exist_ok=True)


@router.post("/upload", status_code=201)
async def upload_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Accept an Excel file upload, persist it, detect the organisation, record
    sheet metadata, and bulk-import all data rows into the staging table.
    """

    # ------------------------------------------------------------------ #
    # Step 1 — Save the file to disk                                      #
    # ------------------------------------------------------------------ #
    try:
        file_path = UPLOAD_DIR / file.filename
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        logger.info("Saved uploaded file to '%s'.", file_path)
    except OSError as exc:
        logger.error("Failed to save '%s': %s", file.filename, exc)
        raise HTTPException(
            status_code=500,
            detail="Could not save the uploaded file.",
        )
    finally:
        file.file.close()

    # ------------------------------------------------------------------ #
    # Step 2 — Detect / create the Organization                           #
    # ------------------------------------------------------------------ #
    try:
        organization = get_or_create_organization(str(file_path), db)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        logger.exception(
            "Unexpected error resolving organization for '%s'.", file.filename
        )
        raise HTTPException(
            status_code=500,
            detail="An error occurred while detecting the organization.",
        )

    # ------------------------------------------------------------------ #
    # Step 3 — Record the upload metadata  (status = UPLOADED)           #
    # ------------------------------------------------------------------ #
    try:
        uploaded_file_service = UploadedFileService(db)
        uploaded_file = uploaded_file_service.record_upload(
            organization_id=organization.id,
            original_filename=file.filename,
            stored_path=str(file_path),
        )
    except FileNotFoundError as exc:
        logger.error(str(exc))
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception:
        logger.exception(
            "Unexpected error recording upload metadata for '%s'.", file.filename
        )
        raise HTTPException(
            status_code=500,
            detail="An error occurred while recording the upload.",
        )

    # ------------------------------------------------------------------ #
    # Step 4 — Ingest sheet metadata                                      #
    # ------------------------------------------------------------------ #
    try:
        uploaded_sheet_service = UploadedSheetService(db)
        sheets = uploaded_sheet_service.ingest_sheets(
            uploaded_file_id=uploaded_file.id,
            file_path=str(file_path),
        )
    except (FileNotFoundError, ValueError) as exc:
        logger.error(str(exc))
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        logger.exception(
            "Unexpected error ingesting sheets for '%s'.", file.filename
        )
        raise HTTPException(
            status_code=500,
            detail="An error occurred while reading workbook sheets.",
        )

    # ------------------------------------------------------------------ #
    # Step 5 — Bulk-ingest staging rows                                   #
    # ------------------------------------------------------------------ #

    # Mark the file as in-progress before the (potentially long) bulk insert
    uploaded_file.upload_status = UploadStatus.PROCESSING
    db.commit()

    try:
        staging_service = StagingRecordService(db)
        total_rows_imported = staging_service.ingest_rows(
            file_path=str(file_path),
            sheets=sheets,
        )
    except Exception:
        # Mark as failed so operators can identify broken imports
        uploaded_file.upload_status = UploadStatus.FAILED
        db.commit()
        logger.exception(
            "Staging import failed for '%s' (uploaded_file_id=%s).",
            file.filename,
            uploaded_file.id,
        )
        raise HTTPException(
            status_code=500,
            detail="An error occurred while importing row data.",
        )

    # All rows inserted successfully — mark the file as fully processed
    uploaded_file.upload_status = UploadStatus.PROCESSED
    db.commit()

    # ------------------------------------------------------------------ #
    # Step 6 — Return response                                            #
    # ------------------------------------------------------------------ #
    return {
        "organization": organization.name,
        "uploaded_file_id": uploaded_file.id,
        "total_sheets": len(sheets),
        "total_rows_imported": total_rows_imported,
        "status": "Imported Successfully",
    }
