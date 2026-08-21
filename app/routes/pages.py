"""
Page Routes
-----------
Server-side rendered HTML pages using Jinja2 templates.

Routes
~~~~~~
GET /                              -> redirect to /upload
GET /upload                        -> upload.html
GET /dashboard                     -> dashboard.html
GET /organizations                 -> organizations.html
GET /organizations/{id}            -> organization_detail.html
GET /uploaded-files                -> uploaded_files.html
GET /sheets-data/{file_id}         -> sheets_data.html
GET /processing-result/{file_id}   -> processing_result.html

NOTE: Starlette >= 0.36 / 1.x changed TemplateResponse to:
    templates.TemplateResponse(request, "name.html", context={...})
  The old form templates.TemplateResponse("name.html", {"request": request, ...})
  raises TypeError: unhashable type: 'dict' due to a broken LRU cache key.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.organization import Organization
from app.models.reconciliation_remark import ReconciliationRemark
from app.models.reconciliation_result import ReconciliationResult
from app.models.staging_record import StagingRecord
from app.models.upload_batch import UploadBatch
from app.models.uploaded_file import UploadedFile, UploadStatus
from app.models.uploaded_sheet import UploadedSheet

logger = logging.getLogger(__name__)

router    = APIRouter(tags=["Pages"])
templates = Jinja2Templates(directory="app/templates")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_file_stats(db: Session) -> dict:
    """Return aggregate counts used on the dashboard."""
    return {
        "total_organizations": db.query(Organization).count(),
        "total_files":         db.query(UploadedFile).count(),
        "uploaded_files":      db.query(UploadedFile).filter(UploadedFile.upload_status == UploadStatus.UPLOADED).count(),
        "processing_files":    db.query(UploadedFile).filter(UploadedFile.upload_status == UploadStatus.PROCESSING).count(),
        "processed_files":     db.query(UploadedFile).filter(UploadedFile.upload_status == UploadStatus.PROCESSED).count(),
        "failed_files":        db.query(UploadedFile).filter(UploadedFile.upload_status == UploadStatus.FAILED).count(),
    }


def _money(value: float | int | None) -> str:
    return f"{float(value or 0):,.2f}"


def _get_reconciliation_dashboard(db: Session) -> dict:
    totals = db.query(
        func.count(ReconciliationResult.id).label("total_pnrs"),
        func.sum(ReconciliationResult.cost_sale).label("cost_sale"),
        func.sum(ReconciliationResult.cost_refund).label("cost_refund"),
        func.sum(ReconciliationResult.cost_net).label("cost_net"),
        func.sum(ReconciliationResult.cashx_amount).label("cashx_sale"),
        func.sum(ReconciliationResult.cashx_refund).label("cashx_refund"),
        func.sum(ReconciliationResult.cashx_net).label("cashx_net"),
        func.sum(ReconciliationResult.spyj_amount).label("spyj_sale"),
        func.sum(ReconciliationResult.spyj_refund).label("spyj_refund"),
        func.sum(ReconciliationResult.spyj_net).label("spyj_net"),
        func.sum(ReconciliationResult.variance).label("variance"),
    ).one()

    total_pnrs = totals.total_pnrs or 0
    cost_net = float(totals.cost_net or 0)
    cashx_net = float(totals.cashx_net or 0)
    spyj_net = float(totals.spyj_net or 0)
    revenue_net = cashx_net + spyj_net
    variance = float(totals.variance or 0)

    remark_rows = (
        db.query(
            ReconciliationRemark.remark,
            func.count(ReconciliationRemark.id).label("count"),
        )
        .join(ReconciliationResult, ReconciliationResult.id == ReconciliationRemark.result_id)
        .group_by(ReconciliationRemark.remark)
        .order_by(func.count(ReconciliationRemark.id).desc())
        .all()
    )
    remarks = [{"remark": row.remark or "Unassigned", "count": row.count or 0} for row in remark_rows]
    remark_map = {item["remark"]: item["count"] for item in remarks}

    sources = [
        {
            "name": "AIR COST TRN",
            "sale": float(totals.cost_sale or 0),
            "refund": float(totals.cost_refund or 0),
            "net": cost_net,
            "percent": 100.0,
        },
        {
            "name": "CASH X",
            "sale": float(totals.cashx_sale or 0),
            "refund": float(totals.cashx_refund or 0),
            "net": cashx_net,
            "percent": (abs(cashx_net) / abs(cost_net) * 100) if cost_net else 0,
        },
        {
            "name": "SPYJ Online",
            "sale": float(totals.spyj_sale or 0),
            "refund": float(totals.spyj_refund or 0),
            "net": spyj_net,
            "percent": (abs(spyj_net) / abs(cost_net) * 100) if cost_net else 0,
        },
    ]

    matched = remark_map.get("Matched", 0)
    variance_count = remark_map.get("Variance", 0)
    missing = max(total_pnrs - matched - variance_count, 0)
    source_counts = db.query(
        func.sum(case((ReconciliationResult.cost_pnr != "not found", 1), else_=0)).label("air_cost"),
        func.sum(case((ReconciliationResult.cashx_pnr != "not found", 1), else_=0)).label("cashx"),
        func.sum(case((ReconciliationResult.spyj_pnr != "not found", 1), else_=0)).label("spyj"),
    ).one()

    air_cost_count = int(source_counts.air_cost or 0)
    cashx_count = int(source_counts.cashx or 0)
    spyj_count = int(source_counts.spyj or 0)

    return {
        "total_pnrs": total_pnrs,
        "cost_net": cost_net,
        "revenue_net": revenue_net,
        "variance": variance,
        "cost_net_display": _money(cost_net),
        "revenue_net_display": _money(revenue_net),
        "variance_display": _money(variance),
        "remarks": remarks,
        "remark_map": remark_map,
        "max_remark_count": max([item["count"] for item in remarks] or [1]),
        "booking_counts": {
            "air_cost": air_cost_count,
            "cashx": cashx_count,
            "spyj": spyj_count,
            "other_portal": spyj_count,
            "matched": matched,
            "variance": variance_count,
            "air_cost_pct": (air_cost_count / total_pnrs * 100) if total_pnrs else 0,
            "cashx_pct": (cashx_count / total_pnrs * 100) if total_pnrs else 0,
            "spyj_pct": (spyj_count / total_pnrs * 100) if total_pnrs else 0,
            "matched_pct": (matched / total_pnrs * 100) if total_pnrs else 0,
            "variance_pct": (variance_count / total_pnrs * 100) if total_pnrs else 0,
        },
        "sources": [
            {
                **source,
                "sale_display": _money(source["sale"]),
                "refund_display": _money(source["refund"]),
                "net_display": _money(source["net"]),
                "percent_display": f"{source['percent']:.2f}%",
            }
            for source in sources
        ],
        "distribution": {
            "matched": matched,
            "variance": variance_count,
            "missing": missing,
            "matched_pct": (matched / total_pnrs * 100) if total_pnrs else 0,
            "variance_pct": (variance_count / total_pnrs * 100) if total_pnrs else 0,
            "missing_pct": (missing / total_pnrs * 100) if total_pnrs else 0,
        },
    }


def _status_value(uploaded_file: UploadedFile) -> str:
    status = uploaded_file.upload_status
    return status.value if hasattr(status, "value") else str(status)


def _combined_status(files: list[UploadedFile]) -> str:
    statuses = {_status_value(f) for f in files}
    if "FAILED" in statuses:
        return "FAILED"
    if "PROCESSING" in statuses:
        return "PROCESSING"
    if statuses == {"PROCESSED"}:
        return "PROCESSED"
    if "UPLOADED" in statuses:
        return "UPLOADED"
    return next(iter(statuses), "UPLOADED")


def _build_uploaded_file_items(files: list[UploadedFile]) -> list[dict]:
    items: list[dict] = []
    grouped: dict[int, list[UploadedFile]] = {}

    for uploaded_file in files:
        if uploaded_file.batch_id:
            grouped.setdefault(uploaded_file.batch_id, []).append(uploaded_file)
        else:
            items.append({
                "is_batch": False,
                "id": uploaded_file.id,
                "name": uploaded_file.original_filename,
                "organization": uploaded_file.organization,
                "organization_id": uploaded_file.organization_id,
                "uploaded_at": uploaded_file.uploaded_at,
                "file_size": uploaded_file.file_size,
                "file_extension": uploaded_file.file_extension,
                "status": _status_value(uploaded_file),
                "file_ids": [uploaded_file.id],
                "files": [uploaded_file],
            })

    for batch_id, batch_files in grouped.items():
        batch = batch_files[0].batch
        uploaded_at = max((f.uploaded_at for f in batch_files if f.uploaded_at), default=None)
        items.append({
            "is_batch": True,
            "id": batch_id,
            "name": batch.name if batch else f"Upload batch #{batch_id}",
            "organization": batch.organization if batch and batch.organization else batch_files[0].organization,
            "organization_id": (
                batch.organization_id
                if batch and batch.organization_id
                else batch_files[0].organization_id
            ),
            "uploaded_at": uploaded_at,
            "file_size": sum(f.file_size or 0 for f in batch_files),
            "file_extension": ".xlsx",
            "status": _combined_status(batch_files),
            "file_ids": [f.id for f in batch_files],
            "files": sorted(batch_files, key=lambda f: f.uploaded_at or f.created_at),
        })

    return sorted(items, key=lambda item: item["uploaded_at"] or datetime.min, reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
# Root → Dashboard redirect
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/", response_class=RedirectResponse)
def root_redirect():
    return RedirectResponse(url="/dashboard")


# ─────────────────────────────────────────────────────────────────────────────
# Upload page
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    return templates.TemplateResponse(request, "upload.html", {
        "active_page": "upload",
    })


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, db: Session = Depends(get_db)):
    stats = _get_file_stats(db)
    recon_summary = _get_reconciliation_dashboard(db)
    recent_files = (
        db.query(UploadedFile)
        .order_by(UploadedFile.uploaded_at.desc())
        .limit(10)
        .all()
    )
    return templates.TemplateResponse(request, "dashboard.html", {
        "active_page":  "dashboard",
        "stats":        stats,
        "recon_summary": recon_summary,
        "recent_files": recent_files,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Organizations list
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/organizations", response_class=HTMLResponse)
def organizations_page(request: Request, db: Session = Depends(get_db)):
    organizations = (
        db.query(Organization)
        .order_by(Organization.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(request, "organizations.html", {
        "active_page":   "organizations",
        "organizations": organizations,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Organization detail
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/organizations/{organization_id}", response_class=HTMLResponse)
def organization_detail_page(
    organization_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    org = db.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    uploaded_files = (
        db.query(UploadedFile)
        .filter(UploadedFile.organization_id == organization_id)
        .order_by(UploadedFile.uploaded_at.desc())
        .all()
    )

    return templates.TemplateResponse(request, "organization_detail.html", {
        "active_page":    "organizations",
        "org":            org,
        "uploaded_files": uploaded_files,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Uploaded Files list
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/uploaded-files", response_class=HTMLResponse)
def uploaded_files_page(
    request:  Request,
    org_id:   int | None = None,
    status:   str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(UploadedFile)

    if org_id:
        query = query.filter(UploadedFile.organization_id == org_id)

    if status:
        try:
            query = query.filter(UploadedFile.upload_status == UploadStatus(status.upper()))
        except ValueError:
            pass

    uploaded_files = query.order_by(UploadedFile.uploaded_at.desc()).all()
    upload_items = _build_uploaded_file_items(uploaded_files)
    organizations  = db.query(Organization).order_by(Organization.name).all()

    return templates.TemplateResponse(request, "uploaded_files.html", {
        "active_page":      "uploaded_files",
        "uploaded_files":   uploaded_files,
        "upload_items":     upload_items,
        "organizations":    organizations,
        "selected_org_id":  org_id,
        "selected_status":  status,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Sheet Data Viewer page
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/sheets-data/{uploaded_file_id}", response_class=HTMLResponse)
def sheets_data_page(
    uploaded_file_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    uploaded_file = (
        db.query(UploadedFile).filter(UploadedFile.id == uploaded_file_id).first()
    )
    if not uploaded_file:
        raise HTTPException(status_code=404, detail="Uploaded file not found")

    file_ids = [uploaded_file.id]
    if uploaded_file.batch_id:
        file_ids = [
            row[0]
            for row in (
                db.query(UploadedFile.id)
                .filter(UploadedFile.batch_id == uploaded_file.batch_id)
                .order_by(UploadedFile.uploaded_at, UploadedFile.id)
                .all()
            )
        ]

    source_sheets = (
        db.query(UploadedSheet)
        .filter(UploadedSheet.uploaded_file_id.in_(file_ids))
        .order_by(UploadedSheet.sheet_index, UploadedSheet.uploaded_file_id)
        .all()
    )

    grouped_sheets: dict[str, dict] = {}
    for sheet in source_sheets:
        item = grouped_sheets.setdefault(
            sheet.sheet_name,
            {
                "id": sheet.id,
                "sheet_name": sheet.sheet_name,
                "sheet_index": sheet.sheet_index,
                "total_rows": 0,
                "total_columns": sheet.total_columns,
                "sheet_ids": [],
            },
        )
        item["total_columns"] = max(item["total_columns"], sheet.total_columns)
        item["sheet_ids"].append(sheet.id)

    for item in grouped_sheets.values():
        item["total_rows"] = (
            db.query(StagingRecord)
            .filter(StagingRecord.uploaded_sheet_id.in_(item["sheet_ids"]))
            .filter(func.json_search(StagingRecord.raw_data, "one", "%_%").isnot(None))
            .count()
        )
        del item["sheet_ids"]

    return templates.TemplateResponse(request, "sheets_data.html", {
        "active_page":   "uploaded_files",
        "uploaded_file": uploaded_file,
        "uploaded_file_ids": file_ids,
        "sheets":        list(grouped_sheets.values()),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Processing Result page
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/processing-result/{uploaded_file_id}", response_class=HTMLResponse)
def processing_result_page(
    uploaded_file_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    uploaded_file = (
        db.query(UploadedFile).filter(UploadedFile.id == uploaded_file_id).first()
    )
    if not uploaded_file:
        raise HTTPException(status_code=404, detail="Uploaded file not found")

    return templates.TemplateResponse(request, "processing_result.html", {
        "active_page":   "uploaded_files",
        "uploaded_file": uploaded_file,
    })
