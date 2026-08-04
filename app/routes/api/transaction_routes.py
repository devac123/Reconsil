"""
Transaction Routes
------------------
Endpoint for triggering the staging-to-transaction transformation.

POST /transactions/process/{uploaded_file_id}
    Read all staging records for the file, apply the organisation's column
    mappings, and write normalised transaction rows.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.uploaded_file import UploadedFile
from app.service.transaction_service import TransactionService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/transactions",
    tags=["Transactions"],
)


@router.post(
    "/process/{uploaded_file_id}",
    status_code=status.HTTP_200_OK,
    summary="Transform staging records into transactions",
    description=(
        "Reads every staging record for the given uploaded file, applies the "
        "organisation's column mappings, and writes normalised transaction rows. "
        "Unmapped columns are discarded. Staging records are marked as processed "
        "upon success. Returns the total count of transactions created."
    ),
)
def process_transactions(
    uploaded_file_id: int = Path(..., gt=0, description="ID of the uploaded file to process"),
    db: Session = Depends(get_db),
):
    # ------------------------------------------------------------------ #
    # 1. Validate the uploaded file exists                                 #
    # ------------------------------------------------------------------ #
    uploaded_file: UploadedFile | None = (
        db.query(UploadedFile)
        .filter(UploadedFile.id == uploaded_file_id)
        .first()
    )
    if uploaded_file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"UploadedFile with id={uploaded_file_id} not found.",
        )

    organization_id: int = uploaded_file.organization_id

    # ------------------------------------------------------------------ #
    # 2. Run the transformation                                            #
    # ------------------------------------------------------------------ #
    try:
        service = TransactionService(db)
        total_processed = service.process_file(
            uploaded_file_id=uploaded_file_id,
            organization_id=organization_id,
        )
    except ValueError as exc:
        # e.g. no sheets found for this file
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except Exception:
        logger.exception(
            "Unexpected error processing transactions for uploaded_file_id=%s.",
            uploaded_file_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while processing transactions.",
        )

    # ------------------------------------------------------------------ #
    # 3. Return summary                                                    #
    # ------------------------------------------------------------------ #
    return {
        "uploaded_file_id":  uploaded_file_id,
        "organization_id":   organization_id,
        "total_processed":   total_processed,
        "status":            "Transactions created successfully",
    }
