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
from app.models.reconciliation_remark import ReconciliationRemark
from app.models.reconciliation_result import ReconciliationResult
from app.models.staging_record import StagingRecord
from app.models.upload_batch import UploadBatch
from app.models.uploaded_file import UploadStatus
from app.models.uploaded_file import UploadedFile
from app.models.uploaded_sheet import UploadedSheet
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
    original_name = Path(upload.filename or "workbook.xlsx").name
    file_path = UPLOAD_DIR / original_name
    if file_path.exists():
        file_path = UPLOAD_DIR / f"{file_path.stem}-{uuid.uuid4().hex[:8]}{file_path.suffix}"
    try:
        with open(file_path, "wb") as buf:
            shutil.copyfileobj(upload.file, buf)
    finally:
        upload.file.close()
    logger.info("Saved uploaded file to '%s'.", file_path)
    return file_path


def _delete_uploaded_files(db: Session, uploaded_files: list[UploadedFile]) -> dict:
    """Delete uploaded files plus dependent sheet, staging, and result data."""
    if not uploaded_files:
        return {"deleted_files": 0, "deleted_sheets": 0, "deleted_staging_records": 0, "deleted_results": 0}

    file_ids = [item.id for item in uploaded_files]
    sheet_ids = [
        row[0]
        for row in (
            db.query(UploadedSheet.id)
            .filter(UploadedSheet.uploaded_file_id.in_(file_ids))
            .all()
        )
    ]
    result_ids = [
        row[0]
        for row in (
            db.query(ReconciliationResult.id)
            .filter(ReconciliationResult.uploaded_file_id.in_(file_ids))
            .all()
        )
    ]

    deleted_remarks = 0
    if result_ids:
        deleted_remarks = (
            db.query(ReconciliationRemark)
            .filter(ReconciliationRemark.result_id.in_(result_ids))
            .delete(synchronize_session=False)
        )

    deleted_results = (
        db.query(ReconciliationResult)
        .filter(ReconciliationResult.uploaded_file_id.in_(file_ids))
        .delete(synchronize_session=False)
    )

    deleted_staging = 0
    if sheet_ids:
        deleted_staging = (
            db.query(StagingRecord)
            .filter(StagingRecord.uploaded_sheet_id.in_(sheet_ids))
            .delete(synchronize_session=False)
        )

    deleted_sheets = (
        db.query(UploadedSheet)
        .filter(UploadedSheet.uploaded_file_id.in_(file_ids))
        .delete(synchronize_session=False)
    )

    stored_paths = [Path(item.file_path) for item in uploaded_files if item.file_path]
    deleted_files = (
        db.query(UploadedFile)
        .filter(UploadedFile.id.in_(file_ids))
        .delete(synchronize_session=False)
    )

    return {
        "deleted_files": deleted_files,
        "deleted_sheets": deleted_sheets,
        "deleted_staging_records": deleted_staging,
        "deleted_results": deleted_results,
        "deleted_remarks": deleted_remarks,
        "_stored_paths": stored_paths,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Background ingestion worker (used by upload-async)
# ─────────────────────────────────────────────────────────────────────────────

def _ingest_saved_file(
    db: Session,
    file_path: Path,
    filename: str,
    job_id: str | None = None,
    finalize_progress: bool = True,
    batch_id: int | None = None,
) -> dict:
    """Ingest one already-saved workbook and return its result payload."""
    organization = get_or_create_organization(str(file_path), db)

    uploaded_file_service = UploadedFileService(db)
    uploaded_file = uploaded_file_service.record_upload(
        organization_id=organization.id,
        original_filename=filename,
        stored_path=str(file_path),
        batch_id=batch_id,
    )

    uploaded_sheet_service = UploadedSheetService(db)
    sheets = uploaded_sheet_service.ingest_sheets(
        uploaded_file_id=uploaded_file.id,
        file_path=str(file_path),
    )

    uploaded_file.upload_status = UploadStatus.PROCESSING
    db.commit()

    staging_service = StagingRecordService(db)
    total_rows = staging_service.ingest_rows(
        file_path=str(file_path),
        sheets=sheets,
        job_id=job_id,
        finalize_progress=finalize_progress,
    )

    uploaded_file.upload_status = UploadStatus.PROCESSED
    db.commit()

    return {
        "organization": organization.name,
        "uploaded_file_id": uploaded_file.id,
        "filename": filename,
        "total_sheets": len(sheets),
        "total_rows_imported": total_rows,
        "sheets": [
            {
                "id": sheet.id,
                "name": sheet.sheet_name,
                "index": sheet.sheet_index,
                "total_rows": sheet.total_rows,
                "total_columns": sheet.total_columns,
            }
            for sheet in sheets
        ],
        "status": "Imported Successfully",
    }


def _run_ingestion(job_id: str, file_path: Path, filename: str) -> None:
    """
    Run the full ingest pipeline in a background thread.
    Opens its own DB session so it is independent of the request session.
    Updates the progress store throughout so the SSE stream has data to emit.
    """
    db: Session = SessionLocal()
    try:
        result = _ingest_saved_file(db, file_path, filename, job_id=job_id)

        # Store the final result payload so the SSE client can display it
        progress_store.update_job(
            job_id,
            status="done",
            percent=100,
            message="Ingestion complete.",
            result=result,
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


def _run_multi_ingestion(job_id: str, saved_files: list[tuple[Path, str]]) -> None:
    """Ingest multiple saved workbooks sequentially under one progress job."""
    db: Session = SessionLocal()
    results: list[dict] = []
    try:
        first_org = get_or_create_organization(str(saved_files[0][0]), db)
        batch = UploadBatch(
            organization_id=first_org.id,
            name=f"Multi-workbook upload ({len(saved_files)} files)",
        )
        db.add(batch)
        db.commit()
        db.refresh(batch)

        for index, (file_path, filename) in enumerate(saved_files, start=1):
            progress_store.update_job(
                job_id,
                status="processing",
                message=f"Processing workbook {index}/{len(saved_files)}: {filename}",
            )
            result = _ingest_saved_file(
                db,
                file_path,
                filename,
                job_id=job_id,
                finalize_progress=False,
                batch_id=batch.id,
            )
            results.append(result)

        uploaded_file_ids = [item["uploaded_file_id"] for item in results]
        progress_store.update_job(
            job_id,
            status="done",
            percent=100,
            message="All workbooks imported.",
            result={
                "organization": ", ".join(sorted({item["organization"] for item in results})),
                "uploaded_file_ids": uploaded_file_ids,
                "uploaded_file_id": uploaded_file_ids[0] if uploaded_file_ids else None,
                "batch_id": batch.id,
                "batch_name": batch.name,
                "total_files": len(results),
                "total_sheets": sum(item["total_sheets"] for item in results),
                "total_rows_imported": sum(item["total_rows_imported"] for item in results),
                "files": results,
                "status": "Imported Successfully",
            },
        )
    except Exception as exc:
        logger.exception("Multi-file ingestion failed for job '%s'.", job_id)
        try:
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

    try:
        result = _ingest_saved_file(db, file_path, file.filename or file_path.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        logger.exception("Staging import failed for '%s'.", file.filename)
        raise HTTPException(status_code=500, detail="Error importing row data.")

    return result


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


@router.post("/upload-multiple-async", status_code=202)
async def upload_multiple_files_async(files: list[UploadFile] = File(...)):
    """
    Save multiple Excel files, start a background ingestion thread, and return
    one job_id. The final progress payload includes all uploaded_file_ids.
    """
    if not files:
        raise HTTPException(status_code=422, detail="Please upload at least one file.")

    saved_files: list[tuple[Path, str]] = []
    try:
        for upload in files:
            saved_files.append((_save_file(upload), upload.filename or "workbook.xlsx"))
    except OSError as exc:
        logger.error("Failed to save one of the uploaded files: %s", exc)
        raise HTTPException(status_code=500, detail="Could not save uploaded files.")

    job_id = str(uuid.uuid4())
    progress_store.create_job(job_id)

    t = threading.Thread(
        target=_run_multi_ingestion,
        args=(job_id, saved_files),
        daemon=True,
        name=f"multi-ingest-{job_id[:8]}",
    )
    t.start()
    logger.info("Started background multi-file ingestion thread for job '%s'.", job_id)

    return {"job_id": job_id}


@router.delete("/{uploaded_file_id}", status_code=200)
def delete_uploaded_file(
    uploaded_file_id: int,
    delete_batch: bool = False,
    db: Session = Depends(get_db),
):
    """
    Delete an uploaded file record and all dependent imported/reconciled data.

    When delete_batch=true and the file belongs to a batch, every file in that
    batch is removed and the UploadBatch row is deleted too.
    """
    uploaded_file = (
        db.query(UploadedFile)
        .filter(UploadedFile.id == uploaded_file_id)
        .first()
    )
    if not uploaded_file:
        raise HTTPException(status_code=404, detail="Uploaded file not found.")

    batch_id = uploaded_file.batch_id if delete_batch else None
    if batch_id:
        files_to_delete = (
            db.query(UploadedFile)
            .filter(UploadedFile.batch_id == batch_id)
            .all()
        )
    else:
        files_to_delete = [uploaded_file]

    try:
        summary = _delete_uploaded_files(db, files_to_delete)
        deleted_batch = False
        if batch_id:
            deleted_batch = (
                db.query(UploadBatch)
                .filter(UploadBatch.id == batch_id)
                .delete(synchronize_session=False)
            ) > 0
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Failed to delete uploaded_file_id=%s.", uploaded_file_id)
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}")

    stored_paths = summary.pop("_stored_paths", [])
    for stored_path in stored_paths:
        try:
            if stored_path.exists() and stored_path.is_file():
                stored_path.unlink()
        except OSError:
            logger.warning("Could not remove stored upload file '%s'.", stored_path, exc_info=True)

    return {
        "status": "deleted",
        "uploaded_file_id": uploaded_file_id,
        "batch_id": batch_id,
        "deleted_batch": deleted_batch,
        **summary,
    }


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
