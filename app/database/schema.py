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

    def _ensure_index(
        table_name: str,
        index_name: str,
        column_sql: str,
        column_names: tuple[str, ...],
    ) -> None:
        inspector.info_cache.clear()
        if table_name not in inspector.get_table_names():
            return

        existing_indexes = inspector.get_indexes(table_name)
        if any(
            index["name"] == index_name
            or tuple(index.get("column_names") or ()) == column_names
            for index in existing_indexes
        ):
            return

        logger.info("Adding %s.%s index.", table_name, index_name)
        with engine.begin() as conn:
            conn.execute(text(
                f"CREATE INDEX {index_name} ON {table_name} ({column_sql})"
            ))
        inspector.info_cache.clear()

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

    _ensure_index("uploaded_files", "ix_uploaded_files_batch_id", "batch_id", ("batch_id",))

    # ── staging_records: uploaded_file_id ────────────────────────────────
    staging_columns = {
        column["name"]
        for column in inspector.get_columns("staging_records")
    }
    staging_indexes = {
        index["name"]
        for index in inspector.get_indexes("staging_records")
    }

    if "uploaded_file_id" not in staging_columns:
        logger.info("Adding staging_records.uploaded_file_id column.")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE staging_records ADD COLUMN uploaded_file_id INT NULL AFTER uploaded_sheet_id"))
            conn.execute(text("CREATE INDEX ix_staging_records_uploaded_file_id ON staging_records (uploaded_file_id)"))
            conn.execute(text("""
                UPDATE staging_records sr
                JOIN uploaded_sheets us ON us.id = sr.uploaded_sheet_id
                SET sr.uploaded_file_id = us.uploaded_file_id
                WHERE sr.uploaded_file_id IS NULL
            """))

    else:
        logger.info("Backfilling staging_records.uploaded_file_id where missing.")
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE staging_records sr
                JOIN uploaded_sheets us ON us.id = sr.uploaded_sheet_id
                SET sr.uploaded_file_id = us.uploaded_file_id
                WHERE sr.uploaded_file_id IS NULL
            """))

    if "ix_staging_records_sheet_row" not in staging_indexes:
        logger.info("Adding staging_records sheet/row composite index.")
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE INDEX ix_staging_records_sheet_row "
                "ON staging_records (uploaded_sheet_id, `row_number`)"
            ))

    _ensure_index(
        "uploaded_sheets",
        "ix_uploaded_sheets_uploaded_file_id",
        "uploaded_file_id",
        ("uploaded_file_id",),
    )
    _ensure_index(
        "staging_records",
        "ix_staging_records_uploaded_file_id",
        "uploaded_file_id",
        ("uploaded_file_id",),
    )
    _ensure_index(
        "reconciliation_results",
        "ix_reconciliation_results_uploaded_file_id",
        "uploaded_file_id",
        ("uploaded_file_id",),
    )

    # ── reconciliation_results: booking_date ──────────────────────────────
    def _recon_columns() -> set[str]:
        inspector.info_cache.clear()
        return {
            column["name"]
            for column in inspector.get_columns("reconciliation_results")
        }

    recon_columns = _recon_columns()

    if "booking_date" not in recon_columns:
        logger.info("Adding reconciliation_results.booking_date column.")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE reconciliation_results ADD COLUMN booking_date DATE NULL AFTER pnr"))

    # Re-read columns in case we just added booking_date above
    recon_columns = _recon_columns()

    if "booking_id" not in recon_columns:
        logger.info("Adding reconciliation_results.booking_id column.")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE reconciliation_results ADD COLUMN booking_id VARCHAR(100) NULL AFTER booking_date"))

    # Re-read columns in case we just added booking_id above
    recon_columns = _recon_columns()

    if "customer_name" not in recon_columns:
        logger.info("Adding reconciliation_results.customer_name column.")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE reconciliation_results ADD COLUMN customer_name VARCHAR(255) NULL AFTER booking_id"))

    # Re-read columns in case we just added customer_name above
    recon_columns = _recon_columns()

    if "cashx_client_name" not in recon_columns:
        logger.info("Adding reconciliation_results.cashx_client_name column.")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE reconciliation_results ADD COLUMN cashx_client_name VARCHAR(255) NULL AFTER cashx_pnr"))

    # Re-read columns in case we just added cashx_client_name above
    recon_columns = _recon_columns()

    if "cashx_client_code" not in recon_columns:
        logger.info("Adding reconciliation_results.cashx_client_code column.")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE reconciliation_results ADD COLUMN cashx_client_code VARCHAR(100) NULL AFTER cashx_client_name"))

    # Re-read columns in case we just added cashx_client_code above
    recon_columns = _recon_columns()

    if "spyj_client_name" not in recon_columns:
        logger.info("Adding reconciliation_results.spyj_client_name column.")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE reconciliation_results ADD COLUMN spyj_client_name VARCHAR(255) NULL AFTER spyj_pnr"))

    # Re-read columns in case we just added spyj_client_name above
    recon_columns = _recon_columns()

    if "spyj_client_code" not in recon_columns:
        logger.info("Adding reconciliation_results.spyj_client_code column.")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE reconciliation_results ADD COLUMN spyj_client_code VARCHAR(100) NULL AFTER spyj_client_name"))

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

    _ensure_index(
        "reconciliation_remarks",
        "ix_reconciliation_remarks_result_id",
        "result_id",
        ("result_id",),
    )
