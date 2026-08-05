"""
Sheet Data Routes
-----------------
API endpoints that expose raw staging rows grouped by sheet.

Endpoints
~~~~~~~~~
GET /files/{uploaded_file_id}/sheets-data
    Returns all sheets for the file, each with a paginated slice of its rows.

GET /files/{uploaded_file_id}/sheets-data/{sheet_id}
    Returns rows for a single sheet with pagination.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.staging_record import StagingRecord
from app.models.uploaded_file import UploadedFile
from app.models.uploaded_sheet import UploadedSheet
from app.repository.staging_record_repository import StagingRecordRepository
from app.repository.uploaded_sheet_repository import UploadedSheetRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["Sheet Data"])


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

def _get_file_or_404(uploaded_file_id: int, db: Session) -> UploadedFile:
    file = db.query(UploadedFile).filter(UploadedFile.id == uploaded_file_id).first()
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"UploadedFile with id={uploaded_file_id} not found.",
        )
    return file


def _sheet_payload(sheet: UploadedSheet, records: list[StagingRecord], total: int, page: int, page_size: int) -> dict:
    """Build the per-sheet dict that is returned in the response."""
    return {
        "sheet_id":     sheet.id,
        "sheet_name":   sheet.sheet_name,
        "sheet_index":  sheet.sheet_index,
        "total_rows":   total,
        "page":         page,
        "page_size":    page_size,
        "total_pages":  max(1, -(-total // page_size)),   # ceiling division
        "columns":      list(records[0].raw_data.keys()) if records else [],
        "rows": [
            {
                "row_number": r.row_number,
                "data":       r.raw_data,
            }
            for r in records
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /files/{uploaded_file_id}/sheets-data
# All sheets in one response, each with first page of rows
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/{uploaded_file_id}/sheets-data",
    status_code=status.HTTP_200_OK,
    summary="List all sheets with their row data for an uploaded file",
    description=(
        "Returns every sheet that belongs to the file. "
        "Each sheet entry contains its metadata and a paginated slice of rows "
        "from the staging table. Use the `page` and `page_size` query params "
        "to navigate rows within each sheet (applied uniformly to all sheets). "
        "For per-sheet independent pagination use the single-sheet endpoint."
    ),
)
def get_all_sheets_data(
    uploaded_file_id: int = Path(..., gt=0, description="ID of the uploaded file"),
    page:      int = Query(1,   ge=1,   description="Page number (1-based)"),
    page_size: int = Query(50,  ge=1, le=500, description="Rows per page"),
    db: Session = Depends(get_db),
):
    _get_file_or_404(uploaded_file_id, db)

    sheet_repo  = UploadedSheetRepository(db)
    record_repo = StagingRecordRepository(db)

    sheets = sheet_repo.get_by_uploaded_file(uploaded_file_id)
    if not sheets:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No sheets found for uploaded_file_id={uploaded_file_id}.",
        )

    skip = (page - 1) * page_size
    result = []

    for sheet in sheets:
        records = record_repo.get_by_sheet(sheet.id, skip=skip, limit=page_size)
        total   = (
            db.query(StagingRecord)
            .filter(StagingRecord.uploaded_sheet_id == sheet.id)
            .count()
        )
        result.append(_sheet_payload(sheet, records, total, page, page_size))

    return {
        "uploaded_file_id": uploaded_file_id,
        "total_sheets":     len(sheets),
        "page":             page,
        "page_size":        page_size,
        "sheets":           result,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /files/{uploaded_file_id}/sheets-data/{sheet_id}
# Single sheet with independent pagination
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/{uploaded_file_id}/sheets-data/{sheet_id}",
    status_code=status.HTTP_200_OK,
    summary="Get paginated row data for a single sheet",
    description=(
        "Returns rows from the staging table for the specified sheet. "
        "Use `page` and `page_size` to paginate through large sheets."
    ),
)
def get_single_sheet_data(
    uploaded_file_id: int = Path(..., gt=0, description="ID of the uploaded file"),
    sheet_id:         int = Path(..., gt=0, description="ID of the sheet"),
    page:      int = Query(1,  ge=1,   description="Page number (1-based)"),
    page_size: int = Query(50, ge=1, le=500, description="Rows per page"),
    db: Session = Depends(get_db),
):
    _get_file_or_404(uploaded_file_id, db)

    sheet = (
        db.query(UploadedSheet)
        .filter(
            UploadedSheet.id == sheet_id,
            UploadedSheet.uploaded_file_id == uploaded_file_id,
        )
        .first()
    )
    if not sheet:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sheet id={sheet_id} not found for file id={uploaded_file_id}.",
        )

    skip    = (page - 1) * page_size
    repo    = StagingRecordRepository(db)
    records = repo.get_by_sheet(sheet.id, skip=skip, limit=page_size)
    total   = (
        db.query(StagingRecord)
        .filter(StagingRecord.uploaded_sheet_id == sheet.id)
        .count()
    )

    return {
        "uploaded_file_id": uploaded_file_id,
        **_sheet_payload(sheet, records, total, page, page_size),
    }
