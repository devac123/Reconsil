#!/usr/bin/env python3
"""
Test to check if staging records are properly loaded - Fixed version
"""

import sys
sys.path.insert(0, '/var/www/html/learning/Reconsil')

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.database.database import DATABASE_URL
from app.database.base import Base

# Import all models to ensure they're registered with SQLAlchemy
from app.models.organization import Organization
from app.models.uploaded_file import UploadedFile
from app.models.uploaded_sheet import UploadedSheet
from app.models.staging_record import StagingRecord
from app.models.reconciliation_result import ReconciliationResult

def check_staging_data():
    # Create database session
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    
    try:
        # First, check if tables exist using SQLAlchemy 2.0 method
        from sqlalchemy import inspect
        inspector = inspect(engine)
        table_names = inspector.get_table_names()
        print(f"Tables in database: {table_names}")
        
        # Check if there are any uploaded files using raw SQL first
        result = db.execute(text("SELECT COUNT(*) as count FROM uploaded_files"))
        file_count = result.fetchone()[0]
        print(f"\nTotal uploaded files: {file_count}")
        
        if file_count == 0:
            print("No files uploaded yet. You need to upload files through the web interface.")
            return
        
        # Now try to use ORM
        uploaded_files = db.query(UploadedFile).all()
        
        for file in uploaded_files:
            print(f"\nFile ID: {file.id}, Name: {file.original_filename}, Status: {file.upload_status}")
            
            # Check sheets for this file
            sheets = db.query(UploadedSheet).filter(UploadedSheet.uploaded_file_id == file.id).all()
            print(f"  Sheets: {len(sheets)}")
            
            for sheet in sheets:
                print(f"    Sheet: '{sheet.sheet_name}' (ID: {sheet.id})")
                
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
                        
                        # Check for key fields in AIR COST TRN
                        if 'air cost' in sheet.sheet_name.lower():
                            print(f"        RecordLocator: {record.raw_data.get('RecordLocator')}")
                            print(f"        PaymentAmount: {record.raw_data.get('PaymentAmount')}")
                            print(f"        Debit or Credit: {record.raw_data.get('Debit or Credit')}")
                            
                            # Calculate net
                            amount = record.raw_data.get('PaymentAmount')
                            dc = record.raw_data.get('Debit or Credit', '').lower()
                            if dc == 'debit':
                                print(f"        Type: Debit, Net would be positive")
                            elif dc == 'credit':
                                print(f"        Type: Credit, Net would be negative")
                        
                        print(f"        Raw data has {len(record.raw_data)} fields")
                
                # Also check reconciliation results
                recon_count = db.query(ReconciliationResult).filter(
                    ReconciliationResult.uploaded_file_id == file.id
                ).count()
                print(f"      Reconciliation results: {recon_count}")
                
                if recon_count > 0:
                    # Check if any have non-zero cost_net
                    zero_net_count = db.query(ReconciliationResult).filter(
                        ReconciliationResult.uploaded_file_id == file.id,
                        ReconciliationResult.cost_net == 0.0
                    ).count()
                    non_zero_count = db.query(ReconciliationResult).filter(
                        ReconciliationResult.uploaded_file_id == file.id,
                        ReconciliationResult.cost_net != 0.0
                    ).count()
                    
                    print(f"      Results with cost_net = 0: {zero_net_count}")
                    print(f"      Results with cost_net != 0: {non_zero_count}")
                    
                    # Show a few sample reconciliation results
                    sample_results = db.query(ReconciliationResult).filter(
                        ReconciliationResult.uploaded_file_id == file.id
                    ).limit(3).all()
                    
                    for i, result in enumerate(sample_results):
                        print(f"      Recon Result {i+1}:")
                        print(f"        PNR: {result.pnr}")
                        print(f"        Cost Net: {result.cost_net}")
                        print(f"        Variance: {result.variance}")
                        print(f"        Remark: {result.remark}")
    
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        db.close()

if __name__ == "__main__":
    check_staging_data()