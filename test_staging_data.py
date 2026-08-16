#!/usr/bin/env python3
"""
Test to check if staging records are properly loaded
"""

import sys
sys.path.insert(0, '/var/www/html/learning/Reconsil')

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database.database import DATABASE_URL
from app.models.uploaded_file import UploadedFile
from app.models.uploaded_sheet import UploadedSheet
from app.models.staging_record import StagingRecord

def check_staging_data():
    # Create database session
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    
    try:
        # Check if there are any uploaded files
        uploaded_files = db.query(UploadedFile).all()
        print(f"Total uploaded files: {len(uploaded_files)}")
        
        for file in uploaded_files:
            print(f"\nFile ID: {file.id}, Name: {file.original_filename}, Status: {file.upload_status}")
            
            # Check sheets for this file
            sheets = db.query(UploadedSheet).filter(UploadedSheet.uploaded_file_id == file.id).all()
            print(f"  Sheets: {len(sheets)}")
            
            for sheet in sheets:
                print(f"    Sheet: {sheet.sheet_name} (ID: {sheet.id})")
                
                # Check staging records for this sheet
                staging_count = db.query(StagingRecord).filter(
                    StagingRecord.uploaded_sheet_id == sheet.id
                ).count()
                print(f"      Staging records: {staging_count}")
                
                # Check a few sample records
                if staging_count > 0:
                    sample_records = db.query(StagingRecord).filter(
                        StagingRecord.uploaded_sheet_id == sheet.id
                    ).limit(3).all()
                    
                    for i, record in enumerate(sample_records):
                        print(f"      Record {i+1}:")
                        print(f"        Row: {record.row_number}")
                        print(f"        PNR: {record.pnr}")
                        print(f"        Raw data keys: {list(record.raw_data.keys())[:5]}...")
                        
                        # Check for key fields in AIR COST TRN
                        if 'air cost' in sheet.sheet_name.lower():
                            print(f"        RecordLocator: {record.raw_data.get('RecordLocator')}")
                            print(f"        PaymentAmount: {record.raw_data.get('PaymentAmount')}")
                            print(f"        Debit or Credit: {record.raw_data.get('Debit or Credit')}")
    
    finally:
        db.close()

if __name__ == "__main__":
    check_staging_data()