#!/usr/bin/env python3
"""
Test specific PNR to understand the reconciliation issue
"""

import sys
sys.path.insert(0, '/var/www/html/learning/Reconsil')

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.database.database import DATABASE_URL
from app.database.base import Base
from app.models.reconciliation_result import ReconciliationResult

def check_specific_pnrs():
    # Create database session
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    
    try:
        # Check some PNRs that should have non-zero cost_net
        print("Checking specific PNRs from AIR COST TRN that should have non-zero cost_net:")
        
        # These are PNRs we saw in the AIR COST TRN sample data
        test_pnrs = ["NQ99SL", "W1H55T", "NRQD6S", "K6UGHB", "A1H4JQ"]
        
        for pnr in test_pnrs:
            result = db.query(ReconciliationResult).filter(
                ReconciliationResult.pnr == pnr
            ).first()
            
            if result:
                print(f"\nPNR: {pnr}")
                print(f"  Cost Net: {result.cost_net}")
                print(f"  Variance: {result.variance}")
                print(f"  Remark: {result.remark}")
                print(f"  Cost PNR: {result.cost_pnr}")
                print(f"  CashX PNR: {result.cashx_pnr}")
                print(f"  SPYJ PNR: {result.spyj_pnr}")
            else:
                print(f"\nPNR {pnr} not found in reconciliation results")
        
        # Now check some PNRs that had cost_net = 0 in the sample
        print("\n\nChecking PNRs that had cost_net = 0 in sample:")
        zero_pnrs = ["I9PLYK", "EZK93S", "K5NJ6R"]
        
        for pnr in zero_pnrs:
            result = db.query(ReconciliationResult).filter(
                ReconciliationResult.pnr == pnr
            ).first()
            
            if result:
                print(f"\nPNR: {pnr}")
                print(f"  Cost Net: {result.cost_net}")
                print(f"  Variance: {result.variance}")
                print(f"  Remark: {result.remark}")
                print(f"  Cost PNR: {result.cost_pnr}")
                print(f"  CashX PNR: {result.cashx_pnr}")
                print(f"  SPYJ PNR: {result.spyj_pnr}")
            else:
                print(f"\nPNR {pnr} not found in reconciliation results")
        
        # Check overall statistics
        print("\n\nOverall reconciliation statistics:")
        
        # Count by remark
        remarks = db.query(
            ReconciliationResult.remark,
            text('COUNT(*) as count')
        ).group_by(ReconciliationResult.remark).all()
        
        print("\nResults by remark:")
        for remark, count in remarks:
            print(f"  {remark}: {count}")
        
        # Check cost_net distribution
        zero_count = db.query(ReconciliationResult).filter(
            ReconciliationResult.cost_net == 0.0
        ).count()
        
        non_zero_count = db.query(ReconciliationResult).filter(
            ReconciliationResult.cost_net != 0.0
        ).count()
        
        print(f"\nCost Net distribution:")
        print(f"  Zero: {zero_count}")
        print(f"  Non-zero: {non_zero_count}")
        
        # Check if zero cost_net always means "Not in Cost"
        zero_not_in_cost = db.query(ReconciliationResult).filter(
            ReconciliationResult.cost_net == 0.0,
            ReconciliationResult.remark == "Not in Cost"
        ).count()
        
        zero_other = db.query(ReconciliationResult).filter(
            ReconciliationResult.cost_net == 0.0,
            ReconciliationResult.remark != "Not in Cost"
        ).count()
        
        print(f"\nWhen cost_net = 0:")
        print(f"  'Not in Cost' remark: {zero_not_in_cost}")
        print(f"  Other remarks: {zero_other}")
        
        # Show some examples of non-zero cost_net
        print(f"\nSome examples with non-zero cost_net:")
        non_zero_examples = db.query(ReconciliationResult).filter(
            ReconciliationResult.cost_net != 0.0
        ).limit(5).all()
        
        for i, result in enumerate(non_zero_examples):
            print(f"\n  Example {i+1}:")
            print(f"    PNR: {result.pnr}")
            print(f"    Cost Net: {result.cost_net}")
            print(f"    Variance: {result.variance}")
            print(f"    Remark: {result.remark}")
    
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        db.close()

if __name__ == "__main__":
    check_specific_pnrs()