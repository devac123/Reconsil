#!/usr/bin/env python3
"""
Debug script to test the reconciliation logic
"""

import pandas as pd
from pathlib import Path
from collections import defaultdict

def _safe_float(value) -> float:
    """Convert *value* to float; return 0.0 for None / non-numeric."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

def debug_cost_aggregation():
    """Test the cost aggregation logic"""
    file_path = Path('test/test.xlsx')
    
    # Read AIR COST TRN sheet
    from app.service.File_reader import FileReaderService
    df = FileReaderService.read_sheet_as_dataframe(file_path, 'AIR COST TRN')
    
    print(f"DataFrame shape: {df.shape}")
    print(f"Columns: {df.columns.tolist()}")
    
    # Check if required columns exist
    required_cols = ['RecordLocator', 'PaymentAmount', 'Debit or Credit']
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        print(f"\nERROR: Missing columns: {missing_cols}")
        print("Available columns:")
        for col in df.columns:
            print(f"  - {col}")
        return
    
    print(f"\nSample data (first 5 rows):")
    print(df[['RecordLocator', 'PaymentAmount', 'Debit or Credit']].head())
    
    # Simulate the aggregation logic
    agg = defaultdict(lambda: {"sale": 0.0, "refund": 0.0})
    
    for idx, row in df.iterrows():
        pnr = str(row.get('RecordLocator') or '').strip()
        if not pnr or pnr.lower() in ("nan", "none", ""):
            continue
            
        amount = _safe_float(row.get('PaymentAmount'))
        dc = str(row.get('Debit or Credit') or '').strip().lower()
        
        print(f"\nRow {idx}: PNR={pnr}, Amount={amount}, Debit/Credit='{dc}'")
        
        if dc == 'debit':
            agg[pnr]["sale"] += amount
            print(f"  → Added to sale: {amount}")
        elif dc == 'credit':
            agg[pnr]["refund"] += abs(amount)
            print(f"  → Added to refund: {abs(amount)}")
        else:
            print(f"  → SKIPPED: Unknown Debit/Credit value: '{dc}'")
    
    # Calculate net for a few PNRs
    print(f"\n\nAggregation results (first 10 PNRs):")
    result = {}
    for i, (pnr, v) in enumerate(agg.items()):
        if i >= 10:
            break
        v["net"] = round(v["sale"] - v["refund"], 2)
        v["sale"] = round(v["sale"], 2)
        v["refund"] = round(v["refund"], 2)
        result[pnr] = v
        print(f"PNR: {pnr}, Sale: {v['sale']}, Refund: {v['refund']}, Net: {v['net']}")
    
    print(f"\nTotal unique PNRs: {len(agg)}")
    
    # Check for PNRs with net = 0
    zero_net_pnrs = [pnr for pnr, v in agg.items() if round(v["sale"] - v["refund"], 2) == 0]
    print(f"\nPNRs with net = 0: {len(zero_net_pnrs)}")
    if zero_net_pnrs:
        print("First 5 PNRs with net = 0:")
        for pnr in zero_net_pnrs[:5]:
            print(f"  {pnr}: Sale={agg[pnr]['sale']}, Refund={agg[pnr]['refund']}")

if __name__ == "__main__":
    debug_cost_aggregation()