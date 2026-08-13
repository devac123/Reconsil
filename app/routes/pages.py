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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.organization import Organization
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


# ─────────────────────────────────────────────────────────────────────────────
# Root → Upload redirect
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/", response_class=RedirectResponse)
def root_redirect():
    return RedirectResponse(url="/upload")


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
    recent_files = (
        db.query(UploadedFile)
        .order_by(UploadedFile.uploaded_at.desc())
        .limit(10)
        .all()
    )
    return templates.TemplateResponse(request, "dashboard.html", {
        "active_page":  "dashboard",
        "stats":        stats,
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
    organizations  = db.query(Organization).order_by(Organization.name).all()

    return templates.TemplateResponse(request, "uploaded_files.html", {
        "active_page":      "uploaded_files",
        "uploaded_files":   uploaded_files,
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

    sheets = (
        db.query(UploadedSheet)
        .filter(UploadedSheet.uploaded_file_id == uploaded_file_id)
        .order_by(UploadedSheet.sheet_index)
        .all()
    )

    return templates.TemplateResponse(request, "sheets_data.html", {
        "active_page":   "uploaded_files",
        "uploaded_file": uploaded_file,
        "sheets":        sheets,
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
