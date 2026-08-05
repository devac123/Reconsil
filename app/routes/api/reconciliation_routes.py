"""
Reconciliation Routes
---------------------
Endpoints that trigger reconciliation and serve the result download.

POST /files/{uploaded_file_id}/reconcile
    Run the reconciliation engine for an uploaded file.
    Returns a JSON summary with row counts.

GET  /files/{uploaded_file_id}/reconcile/download
    Stream an Excel workbook containing the reconciliation result sheet.
    Column layout mirrors the original "Reconcilation" sheet in the
    client workbook.
"""

import io
import logging

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from fastapi import APIRouter, Depends, HTTPException, Path, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.reconciliation_result import ReconciliationResult
from app.models.uploaded_file import UploadedFile
from app.service.reconciliation_service import ReconciliationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["Reconciliation"])


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

def _get_file_or_404(uploaded_file_id: int, db: Session) -> UploadedFile:
    f = db.query(UploadedFile).filter(UploadedFile.id == uploaded_file_id).first()
    if not f:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"UploadedFile id={uploaded_file_id} not found.",
        )
    return f


# ─────────────────────────────────────────────────────────────────────────────
# POST /files/{id}/reconcile
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/{uploaded_file_id}/reconcile",
    status_code=status.HTTP_200_OK,
    summary="Run reconciliation for an uploaded file",
    description=(
        "Triggers the Cost-vs-Revenue reconciliation engine for the given "
        "uploaded file. Any previously stored results for this file are "
        "replaced. Returns a summary with the number of reconciled PNR rows."
    ),
)
def run_reconciliation(
    uploaded_file_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
):
    _get_file_or_404(uploaded_file_id, db)

    try:
        svc = ReconciliationService(db)
        total_rows = svc.reconcile(uploaded_file_id)
    except Exception as exc:
        logger.exception(
            "Reconciliation failed for uploaded_file_id=%s.", uploaded_file_id
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Reconciliation failed: {exc}",
        )

    return {
        "uploaded_file_id": uploaded_file_id,
        "status":           "completed",
        "reconciled_rows":  total_rows,
        "message":          f"Reconciliation complete. {total_rows} PNR rows produced.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /files/{id}/reconcile/download
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/{uploaded_file_id}/reconcile/download",
    summary="Download reconciliation result as Excel",
    description=(
        "Generates and streams an .xlsx file containing the reconciliation "
        "result sheet. The column layout mirrors the original client workbook."
    ),
)
def download_reconciliation(
    uploaded_file_id: int = Path(..., gt=0),
    db: Session = Depends(get_db),
):
    uploaded_file = _get_file_or_404(uploaded_file_id, db)

    results: list[ReconciliationResult] = (
        db.query(ReconciliationResult)
        .filter(ReconciliationResult.uploaded_file_id == uploaded_file_id)
        .order_by(ReconciliationResult.pnr)
        .all()
    )

    if not results:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No reconciliation results found for this file. "
                "Please run reconciliation first (POST /files/{id}/reconcile)."
            ),
        )

    xlsx_bytes = _build_excel(results, uploaded_file)

    filename = (
        f"reconciliation_{uploaded_file.original_filename}"
        .replace(" ", "_")
        .replace(".xlsx", "")
        + "_result.xlsx"
    )

    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Excel builder
# ─────────────────────────────────────────────────────────────────────────────

# ── colours (matching the style of the original sheet) ──
_HDR_FILL_1  = PatternFill("solid", fgColor="1F4E79")   # dark blue  – main groups
_HDR_FILL_2  = PatternFill("solid", fgColor="2E75B6")   # mid  blue  – sub-headers
_HDR_FILL_VAR = PatternFill("solid", fgColor="833C00")  # dark amber – variance
_WHITE_FONT  = Font(bold=True, color="FFFFFF")
_BOLD        = Font(bold=True)
_THIN = Side(border_style="thin", color="CCCCCC")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

_REMARK_COLOURS = {
    "Matched":                PatternFill("solid", fgColor="E2EFDA"),  # light green
    "Markup/Booking Charges": PatternFill("solid", fgColor="FFF2CC"),  # light yellow
    "Not in Cost":            PatternFill("solid", fgColor="FCE4D6"),  # light orange
    "Not in CASH X":          PatternFill("solid", fgColor="FCE4D6"),
    "Not in SPYJ":            PatternFill("solid", fgColor="FCE4D6"),
    "Variance":               PatternFill("solid", fgColor="FFDEDE"),  # light red
}


def _build_excel(
    results: list[ReconciliationResult],
    uploaded_file: UploadedFile,
) -> bytes:
    """Build and return the reconciliation Excel as bytes."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Reconciliation"

    # ── Title rows ──────────────────────────────────────────────────────
    ws.merge_cells("A1:R1")
    title_cell = ws["A1"]
    title_cell.value = "Indigo Reconciliation - Cost vs Revenue"
    title_cell.font  = Font(bold=True, size=14, color="FFFFFF")
    title_cell.fill  = _HDR_FILL_1
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 24

    ws.merge_cells("A2:R2")
    sub_cell = ws["A2"]
    sub_cell.value = f"File: {uploaded_file.original_filename}"
    sub_cell.font  = Font(italic=True, color="595959")
    sub_cell.alignment = Alignment(horizontal="center")

    # ── Group header row (row 3) ─────────────────────────────────────────
    # Columns:
    #  A        = Parental PNR
    #  B–E      = Cost (AIR COST TRN)
    #  F–I      = CASH X (CASH x SAle / CASH X Re)
    #  J–M      = SPYJ Online Sale
    #  N        = Variance
    #  O–Q      = Remarks

    group_headers = [
        (1,  1,  "Parental PNR",   _HDR_FILL_1),
        (2,  5,  "Cost",           _HDR_FILL_2),
        (6,  9,  "CASH X",         _HDR_FILL_2),
        (10, 13, "SPYJ Online Sale",_HDR_FILL_2),
        (14, 14, "VARIANCE",       _HDR_FILL_VAR),
        (15, 17, "Remarks",        _HDR_FILL_1),
    ]

    for start_col, end_col, label, fill in group_headers:
        if start_col == end_col:
            cell = ws.cell(row=3, column=start_col)
            cell.value = label
        else:
            ws.merge_cells(
                start_row=3, start_column=start_col,
                end_row=3,   end_column=end_col,
            )
            cell = ws.cell(row=3, column=start_col)
            cell.value = label

        cell.font      = _WHITE_FONT
        cell.fill      = fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border    = _BORDER

    ws.row_dimensions[3].height = 18

    # ── Column sub-headers (row 4) ───────────────────────────────────────
    col_headers = [
        "Parental PNR",               # A
        "PNR",    "Sale",   "Refund",  "Net",     # B–E  Cost
        "PNR",    "Amount", "Refund",  "Net",     # F–I  CASH X
        "PNR",    "Amount", "Refund",  "Net",     # J–M  SPYJ
        "Cost−CashX−SPYJ",            # N  Variance
        "Remark", "Revised Remark", "Final Remark", # O–Q
    ]

    for col_idx, header in enumerate(col_headers, start=1):
        cell = ws.cell(row=4, column=col_idx, value=header)
        cell.font      = _WHITE_FONT
        cell.fill      = _HDR_FILL_2
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border    = _BORDER

    ws.row_dimensions[4].height = 30

    # ── Data rows (starting at row 5) ────────────────────────────────────
    for row_idx, rec in enumerate(results, start=5):
        remark_fill = _REMARK_COLOURS.get(rec.remark or "", None)

        row_data = [
            rec.pnr,
            rec.cost_pnr,   rec.cost_sale,   rec.cost_refund,   rec.cost_net,
            rec.cashx_pnr,  rec.cashx_amount, rec.cashx_refund, rec.cashx_net,
            rec.spyj_pnr,   rec.spyj_amount,  rec.spyj_refund,  rec.spyj_net,
            rec.variance,
            rec.remark,     rec.revised_remark, rec.final_remark,
        ]

        for col_idx, value in enumerate(row_data, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border = _BORDER
            # Colour the whole row by remark
            if remark_fill:
                cell.fill = remark_fill
            # Right-align numeric columns
            if col_idx in (3, 4, 5, 7, 8, 9, 11, 12, 13, 14):
                cell.alignment = Alignment(horizontal="right")
                if isinstance(value, float):
                    cell.number_format = "#,##0.00"

    # ── Column widths ─────────────────────────────────────────────────────
    widths = {
        1: 14,  # PNR
        2: 12,  3: 12,  4: 12,  5: 12,   # Cost
        6: 12,  7: 12,  8: 12,  9: 12,   # CASH X
        10: 12, 11: 12, 12: 12, 13: 12,  # SPYJ
        14: 16,                           # Variance
        15: 28, 16: 24, 17: 24,           # Remarks
    }
    for col, width in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = width

    # Freeze panes below headers
    ws.freeze_panes = "B5"

    # ── Auto-filter on the data ───────────────────────────────────────────
    ws.auto_filter.ref = f"A4:{get_column_letter(17)}{4 + len(results)}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
