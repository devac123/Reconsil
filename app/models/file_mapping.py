from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class MappingDataType(str, PyEnum):
    """
    Expected data type of the mapped system field.

    Used downstream when transforming or validating staged values.
    """
    STRING = "STRING"
    INTEGER = "INTEGER"
    FLOAT = "FLOAT"
    DATE = "DATE"
    DATETIME = "DATETIME"
    BOOLEAN = "BOOLEAN"


class FileMapping(Base):
    """
    Maps a single Excel column to an internal system field for a given
    organisation and sheet name.

    The natural business key is ``(organization_id, sheet_name, excel_column)``
    — enforced by a unique constraint so the same column cannot be mapped
    twice for the same org/sheet combination.

    Examples
    --------
    ::

        organization_id = 1              # Indigo
        sheet_name      = "Revenue"
        excel_column    = "Booking ID"
        system_field    = "booking_id"
        data_type       = MappingDataType.STRING
        is_required     = True
    """

    __tablename__ = "file_mappings"

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "sheet_name",
            "excel_column",
            name="uq_file_mapping_org_sheet_column",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    organization_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Name of the worksheet tab this mapping applies to
    sheet_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    # Column name exactly as it appears in the Excel header row
    excel_column: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )

    # Internal field name used by the reconciliation engine
    system_field: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    # Expected data type for downstream transformation / validation
    data_type: Mapped[MappingDataType] = mapped_column(
        Enum(MappingDataType),
        nullable=False,
        default=MappingDataType.STRING,
    )

    # Whether this column must be present and non-null in every staging row
    is_required: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    organization = relationship(
        "Organization",
        backref="file_mappings",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<FileMapping(id={self.id}, "
            f"org={self.organization_id}, "
            f"sheet='{self.sheet_name}', "
            f"'{self.excel_column}' -> '{self.system_field}')>"
        )
