from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class UploadStatus(str, PyEnum):
    """Lifecycle states for an uploaded file."""
    UPLOADED = "UPLOADED"       # File saved to disk, record created
    PROCESSING = "PROCESSING"   # Sheets / rows being imported
    PROCESSED = "PROCESSED"     # Import complete
    FAILED = "FAILED"           # An error occurred during processing


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    organization_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Human-readable name the user submitted
    original_filename: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )

    # Name used when writing to disk (may differ if sanitised / de-duplicated)
    stored_filename: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )

    # Relative or absolute path to the file on disk
    file_path: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
    )

    # Size in bytes
    file_size: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    # e.g. ".xlsx", ".xls"
    file_extension: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    upload_status: Mapped[UploadStatus] = mapped_column(
        Enum(UploadStatus),
        nullable=False,
        default=UploadStatus.UPLOADED,
        index=True,
    )

    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
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

    # Relationship (lazy-loaded; avoids circular imports with Organization)
    organization = relationship("Organization", backref="uploaded_files", lazy="select")

    def __repr__(self) -> str:
        return (
            f"<UploadedFile(id={self.id}, "
            f"filename='{self.original_filename}', "
            f"status='{self.upload_status}')>"
        )
