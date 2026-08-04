from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Date,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class StagingRecord(Base):
    """
    Raw Excel rows before reconciliation processing.
    Stores complete original row in JSON plus searchable fields.
    """

    __tablename__ = "staging_records"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    uploaded_sheet_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("uploaded_sheets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    row_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )


    # Frequently searched fields
    pnr: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    ticket_number: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    transaction_date: Mapped[datetime | None] = mapped_column(
        Date,
        nullable=True,
        index=True,
    )


    # Complete original Excel row
    raw_data: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
    )


    is_processed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
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


    uploaded_sheet = relationship(
        "UploadedSheet",
        backref="staging_records",
        lazy="select",
    )


    def __repr__(self) -> str:
        return (
            f"<StagingRecord("
            f"id={self.id}, "
            f"sheet_id={self.uploaded_sheet_id}, "
            f"pnr={self.pnr}, "
            f"row={self.row_number})>"
        )