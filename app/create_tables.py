from app.database.base import Base
from app.database.database import engine

# Import all models
from app.models.organization import Organization
from app.models.upload_batch import UploadBatch
from app.models.uploaded_file import UploadedFile
from app.models.uploaded_sheet import UploadedSheet
from app.models.staging_record import StagingRecord
from app.models.reconciliation_result import ReconciliationResult

Base.metadata.create_all(bind=engine)
