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
from app.models.reconciliation_remark import ReconciliationRemark  # noqa: F401

logger = logging.getLogger(__name__)


def ensure_schema() -> None:
    """Apply lightweight schema additions needed by the current codebase."""
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)

    # ── uploaded_files: batch_id ──────────────────────────────────────────
    uploaded_file_columns = {
        column["name"]
        for column in inspector.get_columns("uploaded_files")
    }

    if "batch_id" not in uploaded_file_columns:
        logger.info("Adding uploaded_files.batch_id for multi-workbook batches.")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE uploaded_files ADD COLUMN batch_id INT NULL"))
            conn.execute(text("CREATE INDEX ix_uploaded_files_batch_id ON uploaded_files (batch_id)"))

    # ── reconciliation_results: booking_date ──────────────────────────────
    recon_columns = {
        column["name"]
        for column in inspector.get_columns("reconciliation_results")
    }

    if "booking_date" not in recon_columns:
        logger.info("Adding reconciliation_results.booking_date column.")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE reconciliation_results ADD COLUMN booking_date DATE NULL AFTER pnr"))

    # Re-read columns in case we just added booking_date above
    recon_columns = {
        column["name"]
        for column in inspector.get_columns("reconciliation_results")
    }

    if "customer_name" not in recon_columns:
        logger.info("Adding reconciliation_results.customer_name column.")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE reconciliation_results ADD COLUMN customer_name VARCHAR(255) NULL AFTER booking_date"))

    # ── reconciliation_remarks: new table ─────────────────────────────────
    existing_tables = inspector.get_table_names()
    if "reconciliation_remarks" not in existing_tables:
        logger.info("Creating reconciliation_remarks table.")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE reconciliation_remarks (
                    id        INT          NOT NULL AUTO_INCREMENT,
                    result_id INT          NOT NULL,
                    remark    VARCHAR(255) NOT NULL,
                    PRIMARY KEY (id),
                    INDEX ix_reconciliation_remarks_id (id),
                    INDEX ix_reconciliation_remarks_result_id (result_id),
                    INDEX ix_reconciliation_remarks_remark (remark),
                    CONSTRAINT fk_recon_remark_result
                        FOREIGN KEY (result_id)
                        REFERENCES reconciliation_results (id)
                        ON DELETE CASCADE
                )
            """))
