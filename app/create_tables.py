from app.database.base import Base
from app.database.database import engine

# Import all models
from app.models.organization import Organization
from app.models.uploaded_file import UploadedFile
from app.models.uploaded_sheet import UploadedSheet
from app.models.staging_record import StagingRecord
from app.models.file_mapping import FileMapping
from app.models.transaction import Transaction

Base.metadata.create_all(bind=engine)