import logging

from sqlalchemy import inspect, text

from app.database.base import Base
from app.database.database import engine

# Import models so SQLAlchemy metadata knows about every table.
from app.models.organization import Organization  # noqa: F401
from app.models.upload_batch import UploadBatch  # noqa: F401
from app.models.uploaded_file import UploadedFile  # noqa: F401
from app.models.uploaded_sheet import UploadedSheet  # noqa: F401
from app.models.staging_record import StagingRecord  # noqa: F401
from app.models.reconciliation_result import ReconciliationResult  # noqa: F401

logger = logging.getLogger(__name__)


def ensure_schema() -> None:
    """Apply lightweight schema additions needed by the current codebase."""
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    uploaded_file_columns = {
        column["name"]
        for column in inspector.get_columns("uploaded_files")
    }

    if "batch_id" in uploaded_file_columns:
        return

    logger.info("Adding uploaded_files.batch_id for multi-workbook batches.")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE uploaded_files ADD COLUMN batch_id INT NULL"))
        conn.execute(text("CREATE INDEX ix_uploaded_files_batch_id ON uploaded_files (batch_id)"))
