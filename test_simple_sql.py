#!/usr/bin/env python3
"""
Simple SQL test to understand the reconciliation issue
"""

import sys
sys.path.insert(0, '/var/www/html/learning/Reconsil')

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.database.database import DATABASE_URL

def check_with_sql():
    # Create database session
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    
    try:
        print("=== Checking reconciliation results ===")
        
        # 1. Overall statistics
        print("\n1. Overall reconciliation statistics:")
        
        # Total results
        result = db.execute(text("SELECT COUNT(*) FROM reconciliation_results"))
        total = result.fetchone()[0]
        print(f"   Total reconciliation results: {total}")
        
        # Results with cost_net = 0 vs != 0
        result = db.execute(text("""
            SELECT 
                SUM(CASE WHEN cost_net = 0 THEN 1 ELSE 0 END) as zero_count,
                SUM(CASE WHEN cost_net != 0 THEN 1 ELSE 0 END) as non_zero_count
            FROM reconciliation_results
        """))
        zero_count, non_zero_count = result.fetchone()
        print(f"   Results with cost_net = 0: {zero_count}")
        print(f"   Results with cost_net != 0: {non_zero_count}")
        
        # 2. Check remark distribution
        print("\n2. Results by remark:")
        result = db.execute(text("""
            SELECT remark, COUNT(*) as count 
            FROM reconciliation_results 
            GROUP BY remark 
            ORDER BY count DESC
        """))
        
        for remark, count in result:
            print(f"   {remark}: {count}")
        
        # 3. Check if PNRs from AIR COST TRN have non-zero cost_net
        print("\n3. Checking specific PNRs that should be in AIR COST TRN:")
        
        # Get some PNRs from staging_records for AIR COST TRN sheet
        result = db.execute(text("""
            SELECT DISTINCT pnr 
            FROM staging_records sr
            JOIN uploaded_sheets us ON sr.uploaded_sheet_id = us.id
            WHERE us.sheet_name LIKE '%AIR COST%'
            LIMIT 10
        """))
        
        air_cost_pnrs = [row[0] for row in result]
        print(f"   Sample PNRs from AIR COST TRN: {air_cost_pnrs}")
        
        # Check their reconciliation results
        for pnr in air_cost_pnrs:
            result = db.execute(text("""
                SELECT cost_net, variance, remark, cost_pnr
                FROM reconciliation_results 
                WHERE pnr = :pnr
                LIMIT 1
            """), {"pnr": pnr})
            
            row = result.fetchone()
            if row:
                cost_net, variance, remark, cost_pnr = row
                print(f"   PNR: {pnr}, Cost Net: {cost_net}, Variance: {variance}, Remark: '{remark}', Cost PNR: '{cost_pnr}'")
            else:
                print(f"   PNR: {pnr} - Not found in reconciliation results")
        
        # 4. Check reconciliation results with non-zero cost_net
        print("\n4. Sample results with NON-ZERO cost_net:")
        result = db.execute(text("""
            SELECT pnr, cost_net, variance, remark, cost_pnr, cashx_pnr, spyj_pnr
            FROM reconciliation_results 
            WHERE cost_net != 0
            LIMIT 10
        """))
        
        for pnr, cost_net, variance, remark, cost_pnr, cashx_pnr, spyj_pnr in result:
            print(f"   PNR: {pnr}")
            print(f"     Cost Net: {cost_net}, Variance: {variance}, Remark: '{remark}'")
            print(f"     Cost PNR: '{cost_pnr}', CashX PNR: '{cashx_pnr}', SPYJ PNR: '{spyj_pnr}'")
        
        # 5. Check reconciliation results with zero cost_net and "Not in Cost" remark
        print("\n5. Sample results with ZERO cost_net and 'Not in Cost' remark:")
        result = db.execute(text("""
            SELECT pnr, cost_net, variance, remark, cost_pnr, cashx_pnr, spyj_pnr
            FROM reconciliation_results 
            WHERE cost_net = 0 AND remark = 'Not in Cost'
            LIMIT 10
        """))
        
        for pnr, cost_net, variance, remark, cost_pnr, cashx_pnr, spyj_pnr in result:
            print(f"   PNR: {pnr}")
            print(f"     Cost Net: {cost_net}, Variance: {variance}, Remark: '{remark}'")
            print(f"     Cost PNR: '{cost_pnr}', CashX PNR: '{cashx_pnr}', SPYJ PNR: '{spyj_pnr}'")
        
        # 6. Check staging records for a specific PNR
        print("\n6. Checking staging records for PNR 'NQ99SL':")
        
        # Check AIR COST TRN
        result = db.execute(text("""
            SELECT sr.raw_data->>'$.RecordLocator' as pnr,
                   sr.raw_data->>'$.PaymentAmount' as amount,
                   sr.raw_data->>'$.Debit or Credit' as type
            FROM staging_records sr
            JOIN uploaded_sheets us ON sr.uploaded_sheet_id = us.id
            WHERE us.sheet_name LIKE '%AIR COST%'
            AND sr.raw_data->>'$.RecordLocator' = 'NQ99SL'
            LIMIT 5
        """))
        
        print("   In AIR COST TRN:")
        for pnr, amount, dc_type in result:
            print(f"     PNR: {pnr}, Amount: {amount}, Type: {dc_type}")
        
        # Check CASH x SAle
        result = db.execute(text("""
            SELECT sr.pnr, sr.raw_data->>'$.Formatted PNR' as formatted_pnr
            FROM staging_records sr
            JOIN uploaded_sheets us ON sr.uploaded_sheet_id = us.id
            WHERE us.sheet_name LIKE '%CASH x SAle%'
            AND (sr.pnr LIKE '%NQ99SL%' OR sr.raw_data->>'$.Formatted PNR' LIKE '%NQ99SL%')
            LIMIT 5
        """))
        
        print("   In CASH x SAle:")
        for pnr, formatted_pnr in result:
            print(f"     PNR: {pnr}, Formatted PNR: {formatted_pnr}")
        
        # Check SPYJ SALE
        result = db.execute(text("""
            SELECT sr.pnr, sr.raw_data->>'$.GDS PNR' as gds_pnr
            FROM staging_records sr
            JOIN uploaded_sheets us ON sr.uploaded_sheet_id = us.id
            WHERE us.sheet_name LIKE '%SPYJ SALE%'
            AND (sr.pnr LIKE '%NQ99SL%' OR sr.raw_data->>'$.GDS PNR' LIKE '%NQ99SL%')
            LIMIT 5
        """))
        
        print("   In SPYJ SALE:")
        for pnr, gds_pnr in result:
            print(f"     PNR: {pnr}, GDS PNR: {gds_pnr}")
    
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        db.close()

if __name__ == "__main__":
    check_with_sql()