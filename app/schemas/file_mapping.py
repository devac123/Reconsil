"""
FileMapping Schemas
-------------------
Pydantic models used for request validation and response serialisation on the
``/file-mapping`` endpoints.

Keeping schemas separate from ORM models ensures the API contract can evolve
independently of the database layer.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.models.file_mapping import MappingDataType


# ────────────────────────────────────────────────────────────────────────────
# Shared / reusable base
# ────────────────────────────────────────────────────────────────────────────

class MappingEntryBase(BaseModel):
    """Fields common to create and update operations for a single mapping."""

    sheet_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Worksheet tab name exactly as it appears in Excel.",
        examples=["Revenue"],
    )
    excel_column: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="Column header exactly as it appears in the Excel file.",
        examples=["Booking ID"],
    )
    system_field: str = Field(
        ...,
        min_length=1,
        max_length=255,
        pattern=r"^[a-z][a-z0-9_]*$",
        description=(
            "Internal snake_case field name used by the reconciliation engine. "
            "Must start with a lowercase letter and contain only lowercase "
            "letters, digits, and underscores."
        ),
        examples=["booking_id"],
    )
    data_type: MappingDataType = Field(
        default=MappingDataType.STRING,
        description="Expected data type for downstream transformation.",
        examples=["STRING"],
    )
    is_required: bool = Field(
        default=False,
        description="Whether this column must be present and non-null in every row.",
    )

    @field_validator("excel_column", "sheet_name", mode="before")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


# ────────────────────────────────────────────────────────────────────────────
# Request schemas
# ────────────────────────────────────────────────────────────────────────────

class MappingEntryCreate(MappingEntryBase):
    """A single column mapping submitted inside a POST /file-mapping request."""
    pass


class FileMappingCreateRequest(BaseModel):
    """
    Request body for ``POST /file-mapping``.

    A caller submits all column mappings for an organisation in one request.
    At least one mapping entry is required.
    """

    organization_id: int = Field(
        ...,
        gt=0,
        description="ID of the organisation these mappings belong to.",
        examples=[1],
    )
    mappings: list[MappingEntryCreate] = Field(
        ...,
        min_length=1,
        description="One or more column mapping definitions.",
    )


# ────────────────────────────────────────────────────────────────────────────
# Response schemas
# ────────────────────────────────────────────────────────────────────────────

class MappingEntryResponse(MappingEntryBase):
    """A single mapping entry returned in API responses."""

    id: int
    organization_id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class FileMappingCreateResponse(BaseModel):
    """Response body for ``POST /file-mapping``."""

    message: str
    organization_id: int
    saved: int = Field(description="Number of mapping entries saved.")
    mappings: list[MappingEntryResponse]


class FileMappingListResponse(BaseModel):
    """Response body for ``GET /file-mapping/{organization_id}``."""

    organization_id: int
    total: int = Field(description="Total number of mappings for this organisation.")

    # Grouped view: { sheet_name: { excel_column: system_field, … }, … }
    mapping_dict: dict[str, dict[str, str]] = Field(
        description=(
            "Nested dict of sheet_name → excel_column → system_field. "
            "Useful for quick lookup during reconciliation."
        ),
        examples=[
            {
                "Revenue": {
                    "Booking ID": "booking_id",
                    "PNR": "pnr",
                    "Travel Date": "travel_date",
                }
            }
        ],
    )
    mappings: list[MappingEntryResponse] = Field(
        description="Full detail for every mapping entry."
    )


class ColumnDiscoveryResponse(BaseModel):
    """
    Response body for ``GET /file-mapping/columns/{organization_id}/{sheet_name}``.

    Lists every unique column name found in staged rows for that org + sheet,
    making it easy for the client to build a mapping UI.
    """

    organization_id: int
    sheet_name: str
    columns: list[str] = Field(
        description="Unique column names discovered in the staging table."
    )
    total: int
