"""
File Upload Routes
------------------
Handles HTTP concerns only. All business logic is delegated to the service
layer.

Endpoints
~~~~~~~~~
POST /files/upload
    Legacy synchronous upload (still works, no progress bar).

POST /files/upload-async
    Saves the file, fires a background thread for ingestion, and immediately
    returns a ``job_id``.  The client can then open the SSE stream below.

GET  /files/progress/{job_id}
    Server-Sent Events stream.  Emits one JSON event per batch committed.
    Closes automatically when the job reaches 'done' or 'failed'.
"""

import json
import logging
import shutil
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database.database import SessionLocal
from app.database.session import get_db
from app.models.uploaded_file import UploadStatus
from app.service import progress_store
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


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _save_file(upload: UploadFile) -> Path:
    """Write *upload* to UPLOAD_DIR and return the path."""
    file_path = UPLOAD_DIR / upload.filename
    try:
        with open(file_path, "wb") as buf:
            shutil.copyfileobj(upload.file, buf)
    finally:
        upload.file.close()
    logger.info("Saved uploaded file to '%s'.", file_path)
    return file_path


# ─────────────────────────────────────────────────────────────────────────────
# Background ingestion worker (used by upload-async)
# ─────────────────────────────────────────────────────────────────────────────

def _run_ingestion(job_id: str, file_path: Path, filename: str) -> None:
    """
    Run the full ingest pipeline in a background thread.
    Opens its own DB session so it is independent of the request session.
    Updates the progress store throughout so the SSE stream has data to emit.
    """
    db: Session = SessionLocal()
    try:
        # ── Organisation ──────────────────────────────────────────────────
        organization = get_or_create_organization(str(file_path), db)

        # ── Record upload ─────────────────────────────────────────────────
        uploaded_file_service = UploadedFileService(db)
        uploaded_file = uploaded_file_service.record_upload(
            organization_id=organization.id,
            original_filename=filename,
            stored_path=str(file_path),
        )

        # ── Sheet metadata ─────────────────────────────────────────────────
        uploaded_sheet_service = UploadedSheetService(db)
        sheets = uploaded_sheet_service.ingest_sheets(
            uploaded_file_id=uploaded_file.id,
            file_path=str(file_path),
        )

        # ── Row ingestion ──────────────────────────────────────────────────
        uploaded_file.upload_status = UploadStatus.PROCESSING
        db.commit()

        staging_service = StagingRecordService(db)
        total_rows = staging_service.ingest_rows(
            file_path=str(file_path),
            sheets=sheets,
            job_id=job_id,
        )

        uploaded_file.upload_status = UploadStatus.PROCESSED
        db.commit()

        # Store the final result payload so the SSE client can display it
        progress_store.update_job(
            job_id,
            status="done",
            percent=100,
            message="Ingestion complete.",
            result={
                "organization":       organization.name,
                "uploaded_file_id":   uploaded_file.id,
                "total_sheets":       len(sheets),
                "total_rows_imported": total_rows,
                "status":             "Imported Successfully",
            },
        )

    except Exception as exc:
        logger.exception("Background ingestion failed for job '%s'.", job_id)
        try:
            # Best-effort: mark file as failed if we got that far
            db.rollback()
        except Exception:
            pass
        progress_store.update_job(
            job_id,
            status="failed",
            message="Ingestion failed.",
            error=str(exc),
        )
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# POST /files/upload  (legacy — synchronous, no progress)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/upload", status_code=201)
async def upload_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Accept an Excel file upload, persist it, detect the organisation, record
    sheet metadata, and bulk-import all data rows into the staging table.
    """

    # ── Save file ──────────────────────────────────────────────────────────
    try:
        file_path = _save_file(file)
    except OSError as exc:
        logger.error("Failed to save '%s': %s", file.filename, exc)
        raise HTTPException(status_code=500, detail="Could not save the uploaded file.")

    # ── Organisation ──────────────────────────────────────────────────────
    try:
        organization = get_or_create_organization(str(file_path), db)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        logger.exception("Error resolving organization for '%s'.", file.filename)
        raise HTTPException(status_code=500, detail="Error detecting organization.")

    # ── Record upload ──────────────────────────────────────────────────────
    try:
        uploaded_file_service = UploadedFileService(db)
        uploaded_file = uploaded_file_service.record_upload(
            organization_id=organization.id,
            original_filename=file.filename,
            stored_path=str(file_path),
        )
    except Exception:
        logger.exception("Error recording upload for '%s'.", file.filename)
        raise HTTPException(status_code=500, detail="Error recording upload.")

    # ── Sheet metadata ─────────────────────────────────────────────────────
    try:
        uploaded_sheet_service = UploadedSheetService(db)
        sheets = uploaded_sheet_service.ingest_sheets(
            uploaded_file_id=uploaded_file.id,
            file_path=str(file_path),
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        logger.exception("Error ingesting sheets for '%s'.", file.filename)
        raise HTTPException(status_code=500, detail="Error reading workbook sheets.")

    # ── Row ingestion ──────────────────────────────────────────────────────
    uploaded_file.upload_status = UploadStatus.PROCESSING
    db.commit()

    try:
        staging_service = StagingRecordService(db)
        total_rows_imported = staging_service.ingest_rows(
            file_path=str(file_path),
            sheets=sheets,
        )
    except Exception:
        uploaded_file.upload_status = UploadStatus.FAILED
        db.commit()
        logger.exception("Staging import failed for '%s'.", file.filename)
        raise HTTPException(status_code=500, detail="Error importing row data.")

    uploaded_file.upload_status = UploadStatus.PROCESSED
    db.commit()

    return {
        "organization":        organization.name,
        "uploaded_file_id":    uploaded_file.id,
        "total_sheets":        len(sheets),
        "total_rows_imported": total_rows_imported,
        "status":              "Imported Successfully",
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /files/upload-async  — returns job_id immediately
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/upload-async", status_code=202)
async def upload_file_async(file: UploadFile = File(...)):
    """
    Save the file, create a progress-store job, start a background thread,
    and return the job_id immediately.  Poll progress via SSE.
    """
    # Save file synchronously (fast — just disk I/O)
    try:
        file_path = _save_file(file)
    except OSError as exc:
        logger.error("Failed to save '%s': %s", file.filename, exc)
        raise HTTPException(status_code=500, detail="Could not save the uploaded file.")

    job_id = str(uuid.uuid4())
    progress_store.create_job(job_id)

    t = threading.Thread(
        target=_run_ingestion,
        args=(job_id, file_path, file.filename),
        daemon=True,
        name=f"ingest-{job_id[:8]}",
    )
    t.start()
    logger.info("Started background ingestion thread for job '%s'.", job_id)

    return {"job_id": job_id}


# ─────────────────────────────────────────────────────────────────────────────
# GET /files/progress/{job_id}  — SSE stream
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/progress/{job_id}")
async def stream_progress(job_id: str):
    """
    Server-Sent Events stream for upload progress.

    Events are JSON objects emitted as:

        data: {"status":"processing","percent":42,...}\\n\\n

    The stream closes automatically when status reaches 'done' or 'failed'.
    """

    def _event_generator():
        last_percent = -1
        # Wait up to 10 s for the job to appear (thread may not have started yet)
        deadline = time.monotonic() + 10
        while progress_store.get_job(job_id) is None:
            if time.monotonic() > deadline:
                yield f"data: {json.dumps({'status': 'failed', 'error': 'job not found'})}\n\n"
                return
            time.sleep(0.2)

        while True:
            job = progress_store.get_job(job_id)
            if job is None:
                break

            # Emit only when something changed (reduces noise)
            if job["percent"] != last_percent or job["status"] in ("done", "failed"):
                last_percent = job["percent"]
                yield f"data: {json.dumps(job)}\n\n"

            if job["status"] in ("done", "failed"):
                # Small delay so the final event is flushed before close
                time.sleep(0.1)
                break

            time.sleep(0.5)   # poll the store every 500 ms

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",   # disables nginx buffering
            "Access-Control-Allow-Origin": "*",
        },
    )
