from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class UploadedSheet(Base):
    """
    Stores metadata for a single worksheet found inside an uploaded Excel file.

    One :class:`~app.models.uploaded_file.UploadedFile` produces N
    ``UploadedSheet`` rows — one per worksheet tab.
    """

    __tablename__ = "uploaded_sheets"

    # A file cannot have two sheets with the same index
    __table_args__ = (
        UniqueConstraint(
            "uploaded_file_id",
            "sheet_index",
            name="uq_uploaded_sheet_file_index",
        ),
    )

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

    # Name of the worksheet tab as it appears in Excel
    sheet_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # Zero-based position of the sheet within the workbook
    sheet_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    # Total number of rows in the sheet (including the header row)
    total_rows: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # Total number of columns detected from the header row
    total_columns: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
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

    # Back-reference to the parent file record
    uploaded_file = relationship(
        "UploadedFile",
        backref="sheets",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<UploadedSheet(id={self.id}, "
            f"file_id={self.uploaded_file_id}, "
            f"index={self.sheet_index}, "
            f"name='{self.sheet_name}')>"
        )
