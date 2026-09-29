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

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database.database import SessionLocal
from app.database.session import get_db
from app.models.organization import Organization
from app.models.staging_record import StagingRecord
from app.models.upload_batch import UploadBatch
from app.models.uploaded_file import UploadStatus
from app.models.uploaded_file import UploadedFile
from app.service import progress_store
from app.service.organization_service import get_or_create_organization
from app.service.staging_record_service import StagingRecordService
from app.service.upload_deletion_service import UploadedFileDeleteNotFoundError
from app.service.upload_deletion_service import UploadedFileDeletionService
from app.service.uploaded_file_service import UploadedFileService
from app.service.uploaded_sheet_service import UploadedSheetService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/files",
    tags=["Files"],
)

UPLOAD_DIR = Path("file")
UPLOAD_DIR.mkdir(exist_ok=True)
MAX_UPLOAD_BATCHES = 2


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


def _batch_limit_message() -> str:
    return (
        "Upload limit reached. The system already has 2 batches. "
        "Please delete an old batch before uploading a new one."
    )


def _ensure_can_create_batch(db: Session) -> None:
    """Block new upload batches once the global batch limit is reached."""
    batch_count = db.query(UploadBatch).count()
    if batch_count >= MAX_UPLOAD_BATCHES:
        raise HTTPException(status_code=422, detail=_batch_limit_message())


def _sheet_columns(db: Session, sheet_id: int) -> list[str]:
    """Return the uploaded column names from the first staged row for a sheet."""
    row = (
        db.query(StagingRecord.raw_data)
        .filter(StagingRecord.uploaded_sheet_id == sheet_id)
        .order_by(StagingRecord.row_number)
        .first()
    )
    if not row or not isinstance(row.raw_data, dict):
        return []
    return list(row.raw_data.keys())


def _create_upload_batch(
    db: Session,
    organization_id: int,
    file_count: int,
) -> UploadBatch:
    """Create one upload batch after enforcing the global batch limit."""
    _ensure_can_create_batch(db)
    batch = UploadBatch(
        organization_id=organization_id,
        name=(
            f"Workbook upload ({file_count} file)"
            if file_count == 1
            else f"Multi-workbook upload ({file_count} files)"
        ),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


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
    organization_id: int | None = None,
) -> dict:
    """Ingest one already-saved workbook and return its result payload."""
    if job_id:
        progress_store.update_job(
            job_id,
            status="processing",
            percent=1,
            message=f"Preparing workbook: {filename or file_path.name}",
        )

    if organization_id is not None:
        organization_record = (
            db.query(Organization)
            .filter(Organization.id == organization_id)
            .first()
        )
        if not organization_record:
            raise ValueError(f"Organization id={organization_id} not found.")
    else:
        organization_record = get_or_create_organization(filename or file_path.name, db)

    batch: UploadBatch | None = None
    if batch_id is None:
        batch = _create_upload_batch(db, organization_record.id, 1)
        batch_id = batch.id
    else:
        batch = db.query(UploadBatch).filter(UploadBatch.id == batch_id).first()
        if not batch:
            raise ValueError(f"Upload batch id={batch_id} not found.")

    resolved_org_id = organization_record.id
    organization_name = organization_record.name

    if job_id:
        progress_store.update_job(
            job_id,
            status="processing",
            percent=2,
            message="Recording upload metadata.",
        )

    uploaded_file_service = UploadedFileService(db)
    uploaded_file = uploaded_file_service.record_upload(
        organization_id=resolved_org_id,
        original_filename=filename,
        stored_path=str(file_path),
        batch_id=batch_id,
    )

    if job_id:
        progress_store.update_job(
            job_id,
            status="processing",
            percent=3,
            message="Scanning workbook sheets.",
        )

    uploaded_sheet_service = UploadedSheetService(db)
    sheets = uploaded_sheet_service.ingest_sheets(
        uploaded_file_id=uploaded_file.id,
        file_path=str(file_path),
    )

    uploaded_file.upload_status = UploadStatus.PROCESSING
    db.commit()

    if job_id:
        progress_store.update_job(
            job_id,
            status="processing",
            percent=5,
            message="Importing worksheet rows.",
        )

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
        "organization": organization_name,
        "uploaded_file_id": uploaded_file.id,
        "batch_id": batch_id,
        "batch_name": batch.name if batch else None,
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
                "columns": _sheet_columns(db, sheet.id),
            }
            for sheet in sheets
        ],
        "status": "Imported Successfully",
    }


def _run_ingestion(
    job_id: str,
    file_path: Path,
    filename: str,
    organization_id: int | None = None,
) -> None:
    """
    Run the full ingest pipeline in a background thread.
    Opens its own DB session so it is independent of the request session.
    Updates the progress store throughout so the SSE stream has data to emit.
    """
    db: Session = SessionLocal()
    try:
        result = _ingest_saved_file(
            db,
            file_path,
            filename,
            job_id=job_id,
            organization_id=organization_id,
        )

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
        error_message = exc.detail if isinstance(exc, HTTPException) else str(exc)
        progress_store.update_job(
            job_id,
            status="failed",
            message="Ingestion failed.",
            error=error_message,
        )
    finally:
        db.close()


def _run_multi_ingestion(
    job_id: str,
    saved_files: list[tuple[Path, str]],
    organization_id: int | None = None,
) -> None:
    """Ingest multiple saved workbooks sequentially under one progress job."""
    db: Session = SessionLocal()
    results: list[dict] = []
    try:
        first_org = (
            db.query(Organization).filter(Organization.id == organization_id).first()
            if organization_id is not None
            else get_or_create_organization(saved_files[0][1], db)
        )
        if not first_org:
            raise ValueError(f"Organization id={organization_id} not found.")
        batch = _create_upload_batch(db, first_org.id, len(saved_files))

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
                organization_id=first_org.id,
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
        error_message = exc.detail if isinstance(exc, HTTPException) else str(exc)
        progress_store.update_job(
            job_id,
            status="failed",
            message="Ingestion failed.",
            error=error_message,
        )
    finally:
        db.close()


def _run_batch_append_ingestion(
    job_id: str,
    batch_id: int,
    saved_files: list[tuple[Path, str]],
) -> None:
    """Ingest saved workbooks into an existing upload batch."""
    db: Session = SessionLocal()
    results: list[dict] = []
    try:
        batch = db.query(UploadBatch).filter(UploadBatch.id == batch_id).first()
        if not batch:
            raise ValueError(f"Upload batch id={batch_id} not found.")
        if batch.organization_id is None:
            raise ValueError("Cannot append files to a batch without an organization.")

        existing_file_ids = [
            row[0]
            for row in (
                db.query(UploadedFile.id)
                .filter(UploadedFile.batch_id == batch_id)
                .order_by(UploadedFile.uploaded_at, UploadedFile.id)
                .all()
            )
        ]

        for index, (file_path, filename) in enumerate(saved_files, start=1):
            progress_store.update_job(
                job_id,
                status="processing",
                message=f"Adding workbook {index}/{len(saved_files)}: {filename}",
            )
            result = _ingest_saved_file(
                db,
                file_path,
                filename,
                job_id=job_id,
                finalize_progress=False,
                batch_id=batch.id,
                organization_id=batch.organization_id,
            )
            results.append(result)

        new_file_ids = [item["uploaded_file_id"] for item in results]
        uploaded_file_ids = existing_file_ids + new_file_ids
        progress_store.update_job(
            job_id,
            status="done",
            percent=100,
            message="Files added to batch.",
            result={
                "organization": batch.organization.name if batch.organization else "—",
                "uploaded_file_ids": uploaded_file_ids,
                "uploaded_file_id": uploaded_file_ids[0] if uploaded_file_ids else None,
                "batch_id": batch.id,
                "batch_name": batch.name,
                "total_files": len(uploaded_file_ids),
                "total_sheets": sum(item["total_sheets"] for item in results),
                "total_rows_imported": sum(item["total_rows_imported"] for item in results),
                "files": results,
                "status": "Imported Successfully",
            },
        )
    except Exception as exc:
        logger.exception("Batch append ingestion failed for job '%s'.", job_id)
        try:
            db.rollback()
        except Exception:
            pass
        progress_store.update_job(
            job_id,
            status="failed",
            message="Batch append failed.",
            error=str(exc),
        )
    finally:
        db.close()


def _run_delete_upload(job_id: str, uploaded_file_id: int, delete_batch: bool) -> None:
    """Delete uploaded data in a background worker so large imports do not block HTTP."""
    db: Session = SessionLocal()

    def _progress(percent: int, message: str) -> None:
        progress_store.update_job(
            job_id,
            status="processing",
            percent=percent,
            message=message,
        )

    try:
        service = UploadedFileDeletionService(db)
        summary = service.delete(
            uploaded_file_id=uploaded_file_id,
            delete_batch=delete_batch,
            progress=_progress,
        )
        progress_store.update_job(
            job_id,
            status="done",
            percent=100,
            message="Delete complete.",
            result=summary,
        )
    except UploadedFileDeleteNotFoundError as exc:
        db.rollback()
        progress_store.update_job(
            job_id,
            status="failed",
            percent=100,
            message="Delete failed.",
            error=str(exc),
        )
    except Exception as exc:
        logger.exception("Failed to delete uploaded_file_id=%s.", uploaded_file_id)
        try:
            db.rollback()
        except Exception:
            pass
        progress_store.update_job(
            job_id,
            status="failed",
            percent=100,
            message="Delete failed.",
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
    organization_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    """
    Accept an Excel file upload, persist it, detect the organisation, record
    sheet metadata, and bulk-import all data rows into the staging table.
    """

    _ensure_can_create_batch(db)

    # ── Save file ──────────────────────────────────────────────────────────
    try:
        file_path = _save_file(file)
    except OSError as exc:
        logger.error("Failed to save '%s': %s", file.filename, exc)
        raise HTTPException(status_code=500, detail="Could not save the uploaded file.")

    try:
        result = _ingest_saved_file(
            db,
            file_path,
            file.filename or file_path.name,
            organization_id=organization_id,
        )
    except HTTPException:
        raise
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
async def upload_file_async(
    file: UploadFile = File(...),
    organization_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    """
    Save the file, create a progress-store job, start a background thread,
    and return the job_id immediately.  Poll progress via SSE.
    """
    _ensure_can_create_batch(db)

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
        args=(job_id, file_path, file.filename, organization_id),
        daemon=True,
        name=f"ingest-{job_id[:8]}",
    )
    t.start()
    logger.info("Started background ingestion thread for job '%s'.", job_id)

    return {"job_id": job_id}


@router.post("/upload-multiple-async", status_code=202)
async def upload_multiple_files_async(
    files: list[UploadFile] = File(...),
    organization_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    """
    Save multiple Excel files, start a background ingestion thread, and return
    one job_id. The final progress payload includes all uploaded_file_ids.
    """
    if not files:
        raise HTTPException(status_code=422, detail="Please upload at least one file.")

    _ensure_can_create_batch(db)

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
        args=(job_id, saved_files, organization_id),
        daemon=True,
        name=f"multi-ingest-{job_id[:8]}",
    )
    t.start()
    logger.info("Started background multi-file ingestion thread for job '%s'.", job_id)

    return {"job_id": job_id}


@router.post("/batches/{batch_id}/upload-async", status_code=202)
async def upload_files_to_batch_async(
    batch_id: int,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """
    Add one or more workbooks to an existing upload batch.

    This appends data to the batch; it does not create a new batch and
    therefore does not count against the global batch limit.
    """
    if not files:
        raise HTTPException(status_code=422, detail="Please upload at least one file.")

    batch = db.query(UploadBatch).filter(UploadBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Upload batch not found.")

    saved_files: list[tuple[Path, str]] = []
    try:
        for upload in files:
            saved_files.append((_save_file(upload), upload.filename or "workbook.xlsx"))
    except OSError as exc:
        logger.error("Failed to save one of the batch append files: %s", exc)
        raise HTTPException(status_code=500, detail="Could not save uploaded files.")

    job_id = str(uuid.uuid4())
    progress_store.create_job(job_id)

    t = threading.Thread(
        target=_run_batch_append_ingestion,
        args=(job_id, batch_id, saved_files),
        daemon=True,
        name=f"batch-append-{job_id[:8]}",
    )
    t.start()
    logger.info(
        "Started background batch append thread for batch_id=%s job '%s'.",
        batch_id,
        job_id,
    )

    return {"job_id": job_id}


@router.delete("/{uploaded_file_id}", status_code=202)
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

    job_id = str(uuid.uuid4())
    progress_store.create_job(job_id)

    t = threading.Thread(
        target=_run_delete_upload,
        args=(job_id, uploaded_file_id, delete_batch),
        daemon=True,
        name=f"delete-upload-{job_id[:8]}",
    )
    t.start()
    logger.info(
        "Started background delete thread for uploaded_file_id=%s job '%s'.",
        uploaded_file_id,
        job_id,
    )

    return {
        "status": "deleting",
        "job_id": job_id,
        "uploaded_file_id": uploaded_file_id,
        "batch_id": uploaded_file.batch_id if delete_batch else None,
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
        terminal_event_sent = False
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
                terminal_event_sent = True
                time.sleep(0.1)
                break

            time.sleep(0.5)   # poll the store every 500 ms

        if terminal_event_sent:
            progress_store.delete_job(job_id)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",   # disables nginx buffering
            "Access-Control-Allow-Origin": "*",
        },
    )
