"""
Sheet Data Routes
-----------------
API endpoints that expose raw staging rows grouped by sheet.

Endpoints
~~~~~~~~~
GET /files/{uploaded_file_id}/sheets-data
    Returns all sheets for the file, each with a paginated slice of its rows.

GET /files/{uploaded_file_id}/sheets-data/{sheet_id}
    Returns rows for a single sheet with filters + pagination.

Filters available on both endpoints
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  pnr              – partial match on the pnr index column
  ticket_number    – partial match on ticket_number index column
  date_from        – transaction_date >= this date (YYYY-MM-DD)
  date_to          – transaction_date <= this date (YYYY-MM-DD)
  is_processed     – true / false
  page             – page number (1-based)
  page_size        – rows per page (max 500)
"""

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session, joinedload

from app.database.session import get_db
from app.models.staging_record import StagingRecord
from app.models.uploaded_file import UploadedFile
from app.models.uploaded_sheet import UploadedSheet

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["Sheet Data"])


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_file_or_404(uploaded_file_id: int, db: Session) -> UploadedFile:
    file = db.query(UploadedFile).filter(UploadedFile.id == uploaded_file_id).first()
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"UploadedFile with id={uploaded_file_id} not found.",
        )
    return file


def _batch_file_ids(uploaded_file: UploadedFile, db: Session) -> list[int]:
    """Return all file IDs that should be viewed as one logical upload."""
    if not uploaded_file.batch_id:
        return [uploaded_file.id]

    rows = (
        db.query(UploadedFile.id)
        .filter(UploadedFile.batch_id == uploaded_file.batch_id)
        .order_by(UploadedFile.uploaded_at, UploadedFile.id)
        .all()
    )
    return [row[0] for row in rows]


def _sheet_group_ids(
    uploaded_file: UploadedFile,
    sheet_id: int,
    db: Session,
) -> tuple[UploadedSheet, list[int]]:
    """Resolve a visible sheet tab to all same-name sheets in the upload batch."""
    file_ids = _batch_file_ids(uploaded_file, db)
    sheet = (
        db.query(UploadedSheet)
        .filter(
            UploadedSheet.id == sheet_id,
            UploadedSheet.uploaded_file_id.in_(file_ids),
        )
        .first()
    )
    if not sheet:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sheet id={sheet_id} not found for file id={uploaded_file.id}.",
        )

    grouped_sheet_ids = [
        row[0]
        for row in (
            db.query(UploadedSheet.id)
            .filter(
                UploadedSheet.uploaded_file_id.in_(file_ids),
                UploadedSheet.sheet_name == sheet.sheet_name,
            )
            .order_by(UploadedSheet.uploaded_file_id, UploadedSheet.sheet_index)
            .all()
        )
    ]
    return sheet, grouped_sheet_ids


def _sheet_payload(
    sheet: UploadedSheet,
    records: list[StagingRecord],
    total: int,
    page: int,
    page_size: int,
) -> dict:
    """Build the per-sheet dict returned in responses."""
    raw_columns = list(records[0].raw_data.keys()) if records else []
    columns = ["File Name", *raw_columns]
    return {
        "sheet_id":    sheet.id,
        "sheet_name":  sheet.sheet_name,
        "sheet_index": sheet.sheet_index,
        "total_rows":  total,
        "page":        page,
        "page_size":   page_size,
        "total_pages": max(1, -(-total // page_size)),
        "columns":     columns,
        "rows": [
            {
                "uploaded_file_id":  r.uploaded_file_id,
                "uploaded_file_name": (
                    r.uploaded_file.original_filename
                    if r.uploaded_file else None
                ),
                "uploaded_sheet_id": r.uploaded_sheet_id,
                "row_number":       r.row_number,
                "pnr":              r.pnr,
                "ticket_number":    r.ticket_number,
                "transaction_date": str(r.transaction_date) if r.transaction_date else None,
                "is_processed":     r.is_processed,
                "data":             {
                    "File Name": (
                        r.uploaded_file.original_filename
                        if r.uploaded_file else f"File #{r.uploaded_file_id}"
                    ),
                    **r.raw_data,
                },
            }
            for r in records
        ],
    }


def _apply_filters(
    q,
    pnr: Optional[str],
    ticket_number: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    is_processed: Optional[bool],
):
    """
    Apply all optional filters to a StagingRecord query.

    For pnr and ticket_number we search BOTH the dedicated indexed column
    AND inside raw_data JSON — so every sheet is searchable regardless of
    whether its field map was configured.
    """
    from sqlalchemy import or_, func, cast
    from sqlalchemy.dialects.mysql import LONGTEXT

    # Ignore staged rows whose raw_data contains no real cell value. This
    # protects older uploads where whitespace-only Excel rows may have been
    # inserted before dataframe cleanup was tightened.
    q = q.filter(func.json_search(StagingRecord.raw_data, "one", "%_%").isnot(None))

    if pnr:
        q = q.filter(
            or_(
                StagingRecord.pnr.ilike(f"%{pnr}%"),
                func.json_search(
                    StagingRecord.raw_data, "one", f"%{pnr}%"
                ).isnot(None),
            )
        )
    if ticket_number:
        q = q.filter(
            or_(
                StagingRecord.ticket_number.ilike(f"%{ticket_number}%"),
                func.json_search(
                    StagingRecord.raw_data, "one", f"%{ticket_number}%"
                ).isnot(None),
            )
        )
    if date_from:
        q = q.filter(StagingRecord.transaction_date >= date_from)
    if date_to:
        q = q.filter(StagingRecord.transaction_date <= date_to)
    if is_processed is not None:
        q = q.filter(StagingRecord.is_processed == is_processed)
    return q


# ─────────────────────────────────────────────────────────────────────────────
# GET /files/{uploaded_file_id}/sheets-data
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/{uploaded_file_id}/sheets-data",
    status_code=status.HTTP_200_OK,
    summary="List all sheets with filtered, paginated row data",
)
def get_all_sheets_data(
    uploaded_file_id: int = Path(..., gt=0),
    # ── Filters ────────────────────────────────────────────────────────
    pnr: Optional[str] = Query(
        None, description="Partial match on PNR (case-insensitive)."
    ),
    ticket_number: Optional[str] = Query(
        None, description="Partial match on ticket number (case-insensitive)."
    ),
    date_from: Optional[date] = Query(
        None, description="Transaction date from (YYYY-MM-DD, inclusive)."
    ),
    date_to: Optional[date] = Query(
        None, description="Transaction date to (YYYY-MM-DD, inclusive)."
    ),
    is_processed: Optional[bool] = Query(
        None, description="Filter by processed status (true / false)."
    ),
    # ── Pagination ─────────────────────────────────────────────────────
    page:      int = Query(1,  ge=1,       description="Page number (1-based)."),
    page_size: int = Query(50, ge=1, le=500, description="Rows per page (max 500)."),
    db: Session = Depends(get_db),
):
    uploaded_file = _get_file_or_404(uploaded_file_id, db)
    file_ids = _batch_file_ids(uploaded_file, db)

    sheets = (
        db.query(UploadedSheet)
        .filter(UploadedSheet.uploaded_file_id.in_(file_ids))
        .order_by(UploadedSheet.sheet_index, UploadedSheet.uploaded_file_id)
        .all()
    )

    if not sheets:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No sheets found for uploaded_file_id={uploaded_file_id}.",
        )

    skip   = (page - 1) * page_size
    result = []
    grouped: dict[str, list[UploadedSheet]] = {}

    for sheet in sheets:
        grouped.setdefault(sheet.sheet_name, []).append(sheet)

    for sheet_group in grouped.values():
        sheet = sheet_group[0]
        sheet_ids = [item.id for item in sheet_group]
        base_q = db.query(StagingRecord).filter(
            StagingRecord.uploaded_sheet_id.in_(sheet_ids)
        )
        base_q = _apply_filters(
            base_q, pnr, ticket_number, date_from, date_to, is_processed
        )

        total   = base_q.count()
        records = (
            base_q
            .options(joinedload(StagingRecord.uploaded_file))
            .order_by(StagingRecord.uploaded_sheet_id, StagingRecord.row_number)
            .offset(skip)
            .limit(page_size)
            .all()
        )
        result.append(_sheet_payload(sheet, records, total, page, page_size))

    return {
        "uploaded_file_id": uploaded_file_id,
        "total_sheets":     len(result),
        "filters": {
            "pnr":          pnr,
            "ticket_number": ticket_number,
            "date_from":    str(date_from) if date_from else None,
            "date_to":      str(date_to)   if date_to   else None,
            "is_processed": is_processed,
        },
        "page":      page,
        "page_size": page_size,
        "sheets":    result,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /files/{uploaded_file_id}/sheets-data/{sheet_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/{uploaded_file_id}/sheets-data/{sheet_id}",
    status_code=status.HTTP_200_OK,
    summary="Get filtered, paginated row data for a single sheet",
)
def get_single_sheet_data(
    uploaded_file_id: int = Path(..., gt=0),
    sheet_id:         int = Path(..., gt=0),
    # ── Filters ────────────────────────────────────────────────────────
    pnr: Optional[str] = Query(
        None, description="Partial match on PNR (case-insensitive)."
    ),
    ticket_number: Optional[str] = Query(
        None, description="Partial match on ticket number (case-insensitive)."
    ),
    date_from: Optional[date] = Query(
        None, description="Transaction date from (YYYY-MM-DD, inclusive)."
    ),
    date_to: Optional[date] = Query(
        None, description="Transaction date to (YYYY-MM-DD, inclusive)."
    ),
    is_processed: Optional[bool] = Query(
        None, description="Filter by processed status (true / false)."
    ),
    # ── Pagination ─────────────────────────────────────────────────────
    page:      int = Query(1,  ge=1,        description="Page number (1-based)."),
    page_size: int = Query(50, ge=1, le=500, description="Rows per page (max 500)."),
    db: Session = Depends(get_db),
):
    uploaded_file = _get_file_or_404(uploaded_file_id, db)
    sheet, grouped_sheet_ids = _sheet_group_ids(uploaded_file, sheet_id, db)

    base_q = db.query(StagingRecord).filter(
        StagingRecord.uploaded_sheet_id.in_(grouped_sheet_ids)
    )
    base_q = _apply_filters(
        base_q, pnr, ticket_number, date_from, date_to, is_processed
    )

    total   = base_q.count()
    skip    = (page - 1) * page_size
    records = (
        base_q
        .options(joinedload(StagingRecord.uploaded_file))
        .order_by(StagingRecord.uploaded_sheet_id, StagingRecord.row_number)
        .offset(skip)
        .limit(page_size)
        .all()
    )

    return {
        "uploaded_file_id": uploaded_file_id,
        "filters": {
            "pnr":           pnr,
            "ticket_number": ticket_number,
            "date_from":     str(date_from) if date_from else None,
            "date_to":       str(date_to)   if date_to   else None,
            "is_processed":  is_processed,
        },
        **_sheet_payload(sheet, records, total, page, page_size),
    }
