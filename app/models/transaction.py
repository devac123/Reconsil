from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class ProcessingStatus(str, PyEnum):
    """
    Lifecycle status of a normalised transaction record.

    PENDING   — created, not yet validated or reconciled
    PROCESSED — successfully passed through all downstream steps
    FAILED    — an error occurred during a downstream step
    """
    PENDING   = "PENDING"
    PROCESSED = "PROCESSED"
    FAILED    = "FAILED"


class Transaction(Base):
    """
    A single normalised data row produced by applying the :class:`FileMapping`
    rules to a raw :class:`~app.models.staging_record.StagingRecord`.

    The ``data`` JSON column contains only the mapped system fields
    (unmapped Excel columns are discarded).  The original raw data is
    preserved in the staging table and can be cross-referenced via
    ``uploaded_file_id`` + ``sheet_name``.

    Example ``data`` value::

        {
            "booking_id": "IND123",
            "pnr":        "ABC123",
            "amount":     4500
        }
    """

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    uploaded_file_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("uploaded_files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    organization_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Sheet that produced this transaction — needed to look up the right mapping
    sheet_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    # Normalised row: { system_field: value, … }
    data: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
    )

    processing_status: Mapped[ProcessingStatus] = mapped_column(
        Enum(ProcessingStatus),
        nullable=False,
        default=ProcessingStatus.PENDING,
        index=True,
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

    uploaded_file = relationship(
        "UploadedFile",
        backref="transactions",
        lazy="select",
    )
    organization = relationship(
        "Organization",
        backref="transactions",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<Transaction(id={self.id}, "
            f"file_id={self.uploaded_file_id}, "
            f"sheet='{self.sheet_name}', "
            f"status='{self.processing_status}')>"
        )
