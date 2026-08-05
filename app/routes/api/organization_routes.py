"""
Organization API Routes
-----------------------
REST endpoints for listing and creating organizations.

Endpoints
~~~~~~~~~
GET  /api/organizations       -> list all organizations
POST /api/organizations       -> create a new organization
GET  /api/organizations/{id}  -> get a single organization
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.organization import Organization

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/organizations", tags=["Organizations"])


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class OrganizationCreate(BaseModel):
    name:        str  = Field(..., min_length=1, max_length=255)
    code:        str  = Field(..., min_length=1, max_length=50)
    description: str | None = Field(None, max_length=2000)
    is_active:   bool = True


class OrganizationResponse(BaseModel):
    id:          int
    name:        str
    code:        str
    description: str | None
    is_active:   bool

    class Config:
        from_attributes = True


# ─────────────────────────────────────────────────────────────────────────────
# List
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[OrganizationResponse], summary="List all organizations")
def list_organizations(db: Session = Depends(get_db)):
    return db.query(Organization).order_by(Organization.created_at.desc()).all()


# ─────────────────────────────────────────────────────────────────────────────
# Get by ID
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{organization_id}", response_model=OrganizationResponse, summary="Get organization by ID")
def get_organization(organization_id: int, db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Organization with id={organization_id} not found.")
    return org


# ─────────────────────────────────────────────────────────────────────────────
# Create
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=OrganizationResponse,
    summary="Create a new organization",
)
def create_organization(body: OrganizationCreate, db: Session = Depends(get_db)):
    # Check for duplicate name or code
    existing_name = db.query(Organization).filter(
        Organization.name.ilike(body.name)
    ).first()
    if existing_name:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An organization with the name '{body.name}' already exists.",
        )

    existing_code = db.query(Organization).filter(
        Organization.code == body.code.upper()
    ).first()
    if existing_code:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An organization with the code '{body.code.upper()}' already exists.",
        )

    try:
        org = Organization(
            name=body.name.strip(),
            code=body.code.strip().upper(),
            description=body.description,
            is_active=body.is_active,
        )
        db.add(org)
        db.commit()
        db.refresh(org)
        logger.info("Created organization '%s' (id=%s)", org.name, org.id)
        return org
    except IntegrityError as exc:
        db.rollback()
        logger.error("IntegrityError creating organization: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An organization with this name or code already exists.",
        )
    except Exception:
        db.rollback()
        logger.exception("Unexpected error creating organization.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while creating the organization.",
        )
