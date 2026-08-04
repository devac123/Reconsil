from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.organization import Organization
from app.models.uploaded_sheet import UploadedSheet
from app.models.staging_record import StagingRecord

router = APIRouter(tags=["Views"])

templates = Jinja2Templates(directory="app/templates")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    
    organizations = (
        db.query(Organization)
        .order_by(Organization.id.asc())
        .all()
    )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {},
    )


@router.get("/organizations", response_class=HTMLResponse)
def organization_list(
    request: Request,
    db: Session = Depends(get_db),
):
    organizations = (
        db.query(Organization)
        .order_by(Organization.id.desc())
        .all()
    )

    return templates.TemplateResponse(
        request,
        "organizations/list.html",
        {
            "organizations": organizations,
        },
    )


@router.get("/sheets", response_class=HTMLResponse)
def Sheet_list(
    request: Request,
    db: Session = Depends(get_db),
):
    sheets = (
        db.query(UploadedSheet)
        .order_by(UploadedSheet.id.desc())
        .all()
    )

    return templates.TemplateResponse(
        request,
        "organizations/sheets_list.html",
        {
            "sheets": sheets,
        },
    )


@router.get("/sheet/{sheet_id}", response_class=HTMLResponse)
def Sheet_detail(
    request: Request,
    sheet_id: int,
    db: Session = Depends(get_db),
):
    # print("sheet_id:", sheet_id)
    sheet_recoards = (
        db.query(StagingRecord)
        .filter(StagingRecord.uploaded_sheet_id == sheet_id)
        .limit(5)
        .first()
    )
    print("sheet_recoards:", sheet_recoards)

    return templates.TemplateResponse(
        request,
        "organizations/sheet_detail.html",
        {
            "sheet_record": sheet_recoards,
        },
    )       
       
