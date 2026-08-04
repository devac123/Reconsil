"""
FileMapping Routes
------------------
REST endpoints for managing column mappings.

Endpoints
~~~~~~~~~
POST /file-mapping
    Save (create or update) one or more column mappings for an organisation.

GET  /file-mapping/{organization_id}
    Return all mappings for an organisation, including a grouped lookup dict.

GET  /file-mapping/columns/{organization_id}/{sheet_name}
    Discover the unique column names present in staged rows for a given
    org + sheet — useful for building a mapping UI without prior knowledge
    of the file structure.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.organization import Organization
from app.schemas.file_mapping import (
    ColumnDiscoveryResponse,
    FileMappingCreateRequest,
    FileMappingCreateResponse,
    FileMappingListResponse,
    MappingEntryResponse,
)
from app.service.file_mapping_service import FileMappingService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/file-mapping",
    tags=["File Mapping"],
)


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _get_organization_or_404(organization_id: int, db: Session) -> Organization:
    """Raise 404 if the organisation does not exist."""
    org = db.query(Organization).filter(Organization.id == organization_id).first()
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id={organization_id} not found.",
        )
    return org


# ────────────────────────────────────────────────────────────────────────────
# POST /file-mapping
# ────────────────────────────────────────────────────────────────────────────

@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=FileMappingCreateResponse,
    summary="Save column mappings for an organisation",
    description=(
        "Create or update one or more column mappings. "
        "If a mapping for the same (organization, sheet, excel_column) "
        "already exists it is updated; otherwise a new record is created. "
        "All entries are saved in a single atomic transaction."
    ),
)
def save_file_mappings(
    body: FileMappingCreateRequest,
    db: Session = Depends(get_db),
) -> FileMappingCreateResponse:

    # Validate that the organisation exists
    _get_organization_or_404(body.organization_id, db)

    try:
        service = FileMappingService(db)
        saved = service.save_mappings(
            organization_id=body.organization_id,
            entries=body.mappings,
        )
    except IntegrityError as exc:
        logger.error(
            "IntegrityError saving mappings for org_id=%s: %s",
            body.organization_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A database constraint was violated while saving mappings.",
        )
    except Exception:
        logger.exception(
            "Unexpected error saving mappings for org_id=%s.",
            body.organization_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while saving column mappings.",
        )

    return FileMappingCreateResponse(
        message="Column mappings saved successfully.",
        organization_id=body.organization_id,
        saved=len(saved),
        mappings=[MappingEntryResponse.model_validate(r) for r in saved],
    )


# ────────────────────────────────────────────────────────────────────────────
# GET /file-mapping/{organization_id}
# ────────────────────────────────────────────────────────────────────────────

@router.get(
    "/{organization_id}",
    status_code=status.HTTP_200_OK,
    response_model=FileMappingListResponse,
    summary="List all mappings for an organisation",
    description=(
        "Returns the full list of column mappings together with a nested "
        "``mapping_dict`` grouped by sheet name for quick lookup."
    ),
)
def get_file_mappings(
    organization_id: int = Path(..., gt=0, description="Organisation ID"),
    db: Session = Depends(get_db),
) -> FileMappingListResponse:

    _get_organization_or_404(organization_id, db)

    try:
        service = FileMappingService(db)
        records = service.get_mappings_for_organization(organization_id)
        mapping_dict = service.get_mapping_dict(organization_id)
    except Exception:
        logger.exception(
            "Unexpected error fetching mappings for org_id=%s.", organization_id
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while retrieving column mappings.",
        )

    return FileMappingListResponse(
        organization_id=organization_id,
        total=len(records),
        mapping_dict=mapping_dict,
        mappings=[MappingEntryResponse.model_validate(r) for r in records],
    )


# ────────────────────────────────────────────────────────────────────────────
# GET /file-mapping/columns/{organization_id}/{sheet_name}
# ────────────────────────────────────────────────────────────────────────────

@router.get(
    "/columns/{organization_id}/{sheet_name}",
    status_code=status.HTTP_200_OK,
    response_model=ColumnDiscoveryResponse,
    summary="Discover staged columns for an organisation + sheet",
    description=(
        "Inspects the staging table and returns all unique column names "
        "found in raw_data for the given organisation and sheet name. "
        "Use this to populate a mapping UI before submitting POST /file-mapping."
    ),
)
def discover_columns(
    organization_id: int = Path(..., gt=0, description="Organisation ID"),
    sheet_name: str = Path(..., min_length=1, description="Sheet name"),
    db: Session = Depends(get_db),
) -> ColumnDiscoveryResponse:

    _get_organization_or_404(organization_id, db)

    try:
        service = FileMappingService(db)
        columns = service.get_columns_from_staging(organization_id, sheet_name)
    except Exception:
        logger.exception(
            "Unexpected error discovering columns for org_id=%s, sheet='%s'.",
            organization_id,
            sheet_name,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while discovering columns.",
        )

    return ColumnDiscoveryResponse(
        organization_id=organization_id,
        sheet_name=sheet_name,
        columns=columns,
        total=len(columns),
    )
