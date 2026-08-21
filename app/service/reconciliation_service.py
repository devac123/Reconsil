"""
Reconciliation Service
----------------------
Implements the core Indigo Cost-vs-Revenue reconciliation logic.

Algorithm (mirrors the original "Reconcilation" sheet)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For a given uploaded file the engine:

1.  Loads staged rows for the 5 source sheets from the DB.
2.  Builds per-PNR aggregates for each data source:

    COST side  (AIR COST TRN)
        PNR key   : RecordLocator
        Sale amt  : sum of PaymentAmount where Debit or Credit == "Debit"
        Refund amt: sum of abs(PaymentAmount) where Debit or Credit == "Credit"
        Net       : sale − refund

    CASH X side
        Sale  (CASH x SAle)  → PNR key: Formatted PNR,  amount: GROSS FARE
        Refund(CASH X Re)    → PNR key: PNR formatted,  amount: GROSS FARE
        Net   = sale − refund

    SPYJ side
        Sale  (SPYJ SALE)    → PNR key: GDS PNR,  amount: Total Amount
        Refund(SPJY Refund)  → PNR key: GDS PNR,  amount: Total Refund Amount
        Net   = sale − refund

3.  Collects every unique PNR across all three data sources.
4.  For each PNR computes:
        variance = cost_net − cashx_net − spyj_net
5.  Assigns a remark using the same rules visible in the sample data:
        Missing from one or more sources → comma-joined labels, e.g.
            "Not in SPYJ, Not in CASH X"  (all missing sources are listed)
        All sources present:
            |variance| < 1   → "Matched"
            300 ± 10         → "Markup/Booking Charges"
            otherwise        → "Variance"
6.  Bulk-inserts all result rows into ``reconciliation_results``.
7.  Returns the count of rows produced.
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import insert, text
from sqlalchemy.orm import Session

from app.models.reconciliation_result import ReconciliationResult
from app.models.reconciliation_remark import ReconciliationRemark
from app.models.uploaded_file import UploadedFile
from app.models.uploaded_sheet import UploadedSheet
from app.models.staging_record import StagingRecord

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 500

# ---------------------------------------------------------------------------
# Sheet-name constants (normalised)
# ---------------------------------------------------------------------------
_SHEET_AIR_COST   = "air cost trn"
_SHEET_CASHX_SALE = "cash x sale"
_SHEET_CASHX_RE   = "cash x re"
_SHEET_SPYJ_SALE  = "spyj sale"
_SHEET_SPJY_REF   = "spjy refund"

# ---------------------------------------------------------------------------
# Remark assignment thresholds
# ---------------------------------------------------------------------------
_MARKUP_VALUE   = 300.0
_MARKUP_TOL     = 10.0   # ±10 around 300 → "Markup/Booking Charges"
_MATCH_TOL      = 1.0    # |variance| < 1 → "Matched"


def _safe_float(value) -> float:
    """Convert *value* to float; return 0.0 for None / non-numeric."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_date(value) -> date | None:
    """Convert common Excel/pandas/string date values to a Python date."""
    if value is None:
        return None
    try:
        import pandas as _pd
        if _pd.isna(value):
            return None
        if isinstance(value, _pd.Timestamp):
            return value.date()
    except (ImportError, TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("nan", "none", "nat"):
            return None
        try:
            return datetime.fromisoformat(s[:10]).date()
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            n = int(value)
            if 1 <= n <= 2958465:
                return date(1899, 12, 30) + timedelta(days=n)
        except (ValueError, OverflowError):
            return None
    return None


def _assign_remarks(
    variance: float,
    cost_found: bool,
    cashx_found: bool,
    spyj_found: bool,
) -> list[str]:
    """Return a list of remark labels for a reconciled PNR row.

    When a PNR is absent from multiple sources every missing-source label
    is included, e.g. ["Not in SPYJ", "Not in CASH X"].
    The "Matched" / "Markup/Booking Charges" / "Variance" labels are only
    assigned when all three sources have data.
    """
    missing: list[str] = []
    if not cost_found:
        missing.append("Not in Cost")
    if not cashx_found:
        missing.append("Not in CASH X")
    if not spyj_found:
        missing.append("Not in SPYJ")

    if missing:
        return missing

    if abs(variance) < _MATCH_TOL:
        return ["Matched"]
    if abs(abs(variance) - _MARKUP_VALUE) <= _MARKUP_TOL:
        return ["Markup/Booking Charges"]
    return ["Variance"]


class ReconciliationService:
    """
    Runs the full Cost-vs-Revenue reconciliation for one uploaded file and
    persists the results to ``reconciliation_results``.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def reconcile(self, uploaded_file_id: int) -> int:
        """
        Run reconciliation for *uploaded_file_id*.

        Deletes any existing results for this file, recomputes, and inserts
        fresh rows.

        Returns
        -------
        int
            Number of reconciled PNR rows produced.
        """
        return self.reconcile_combined([uploaded_file_id], result_uploaded_file_id=uploaded_file_id)

    def reconcile_combined(
        self,
        uploaded_file_ids: list[int],
        result_uploaded_file_id: int | None = None,
    ) -> int:
        """
        Run reconciliation across one or more uploaded workbooks.

        Rows from sheets with the same logical name are combined before
        aggregation, so AIR COST TRN, CASH X sale/refund, and SPYJ sale/refund
        can be split across multiple uploaded files.
        """
        uploaded_file_ids = list(dict.fromkeys(uploaded_file_ids))
        if not uploaded_file_ids:
            raise ValueError("At least one uploaded_file_id is required.")

        result_file_id = result_uploaded_file_id or uploaded_file_ids[0]
        if result_file_id not in uploaded_file_ids:
            uploaded_file_ids.insert(0, result_file_id)

        existing_ids = {
            file_id
            for (file_id,) in (
                self._db.query(UploadedFile.id)
                .filter(UploadedFile.id.in_(uploaded_file_ids))
                .all()
            )
        }
        missing_ids = [file_id for file_id in uploaded_file_ids if file_id not in existing_ids]
        if missing_ids:
            raise ValueError(f"Uploaded file id(s) not found: {missing_ids}")

        logger.info(
            "Starting combined reconciliation for uploaded_file_ids=%s; result_file_id=%s.",
            uploaded_file_ids,
            result_file_id,
        )

        # Remove stale results from a previous run (if any)
        self._db.execute(
            text(
                "DELETE FROM reconciliation_results "
                "WHERE uploaded_file_id = :fid"
            ),
            {"fid": result_file_id},
        )

        # Load sheet-id map: normalised_sheet_name → list[sheet_id]
        sheet_map = self._load_sheet_map(uploaded_file_ids)
        logger.info("Sheet map: %s", {k: v for k, v in sheet_map.items()})

        # Build aggregates per data source
        cost_agg   = self._aggregate_cost(sheet_map)
        cashx_sale = self._aggregate_gross_fare(sheet_map, _SHEET_CASHX_SALE)
        cashx_re   = self._aggregate_gross_fare(sheet_map, _SHEET_CASHX_RE)
        spyj_sale  = self._aggregate_spyj_sale(sheet_map)
        spyj_ref   = self._aggregate_spyj_refund(sheet_map)

        # CASH X net = sale - refund  (keyed by PNR)
        cashx_agg = self._merge_sale_refund(cashx_sale, cashx_re)

        # SPYJ net = sale - refund
        spyj_agg = self._merge_sale_refund(spyj_sale, spyj_ref)

        # Union of all PNRs
        all_pnrs = (
            set(cost_agg.keys())
            | set(cashx_agg.keys())
            | set(spyj_agg.keys())
        )

        logger.info("Total unique PNRs to reconcile: %s", len(all_pnrs))

        # Build result rows
        rows = []
        now = datetime.utcnow()

        for pnr in all_pnrs:
            c = cost_agg.get(pnr)
            x = cashx_agg.get(pnr)
            s = spyj_agg.get(pnr)

            cost_sale   = c["sale"]   if c else 0.0
            cost_refund = c["refund"] if c else 0.0
            cost_net    = c["net"]    if c else 0.0

            cashx_amount = x["sale"]   if x else 0.0
            cashx_refund = x["refund"] if x else 0.0
            cashx_net    = x["net"]    if x else 0.0

            spyj_amount = s["sale"]   if s else 0.0
            spyj_refund = s["refund"] if s else 0.0
            spyj_net    = s["net"]    if s else 0.0

            variance = cost_net - cashx_net - spyj_net

            remarks = _assign_remarks(
                variance,
                cost_found=bool(c),
                cashx_found=bool(x),
                spyj_found=bool(s),
            )
            # Keep the display-cache string in the remark column
            remark_str = ", ".join(remarks)

            rows.append({
                "uploaded_file_id": result_file_id,
                "pnr":              pnr,
                "booking_date":     c["booking_date"] if c else None,
                "customer_name":    c["customer_name"] if c else None,

                "cost_pnr":    pnr if c else "not found",
                "cost_sale":   cost_sale,
                "cost_refund": cost_refund,
                "cost_net":    cost_net,

                "cashx_pnr":    pnr if x else "not found",
                "cashx_amount": cashx_amount,
                "cashx_refund": cashx_refund,
                "cashx_net":    cashx_net,

                "spyj_pnr":    pnr if s else "not found",
                "spyj_amount": spyj_amount,
                "spyj_refund": spyj_refund,
                "spyj_net":    spyj_net,

                "variance":       round(variance, 2),
                "remark":         remark_str,
                "revised_remark": None,
                "final_remark":   None,

                "created_at": now,
                "updated_at": now,

                # Carry the individual labels so we can insert remark rows below
                "_remarks": remarks,
            })

        # Bulk-insert in chunks
        total_inserted = self._bulk_insert(rows)

        # Now insert individual remark rows for every result
        self._bulk_insert_remarks(rows)

        self._db.commit()
        logger.info(
            "Combined reconciliation complete for file_ids=%s: %s rows produced.",
            uploaded_file_ids,
            total_inserted,
        )
        return total_inserted

    # ------------------------------------------------------------------ #
    # Sheet loading helpers                                                #
    # ------------------------------------------------------------------ #

    def _load_sheet_map(self, uploaded_file_ids: list[int]) -> dict[str, list[int]]:
        """
        Return normalised_sheet_name → sheet_id list for uploaded workbooks.
        """
        sheets = (
            self._db.query(UploadedSheet)
            .filter(UploadedSheet.uploaded_file_id.in_(uploaded_file_ids))
            .order_by(UploadedSheet.uploaded_file_id, UploadedSheet.sheet_index)
            .all()
        )
        sheet_map: dict[str, list[int]] = defaultdict(list)
        for sheet in sheets:
            sheet_map[sheet.sheet_name.strip().lower()].append(sheet.id)
        return dict(sheet_map)

    def _iter_rows(self, sheet_ids: list[int] | None):
        """
        Yield raw_data dicts for every staging record in *sheet_ids*.
        Uses chunked iteration to stay memory-efficient.
        """
        if not sheet_ids:
            return

        for sheet_id in sheet_ids:
            offset = 0
            while True:
                chunk = (
                    self._db.query(StagingRecord.raw_data)
                    .filter(StagingRecord.uploaded_sheet_id == sheet_id)
                    .order_by(StagingRecord.row_number)
                    .offset(offset)
                    .limit(_CHUNK_SIZE)
                    .all()
                )
                if not chunk:
                    break
                for (raw_data,) in chunk:
                    yield raw_data
                if len(chunk) < _CHUNK_SIZE:
                    break
                offset += _CHUNK_SIZE

    # ------------------------------------------------------------------ #
    # Per-source aggregation                                               #
    # ------------------------------------------------------------------ #

    def _aggregate_cost(self, sheet_map: dict) -> dict[str, dict]:
        """
        Aggregate AIR COST TRN rows by RecordLocator.

        Debit  rows → add to sale.
        Credit rows → add to refund (store as positive).
        Also captures BookingDate and customer name (Name1) per PNR.
        """
        sheet_ids = sheet_map.get(_SHEET_AIR_COST)
        agg: dict[str, dict] = defaultdict(
            lambda: {"sale": 0.0, "refund": 0.0, "booking_date": None, "customer_name": None}
        )

        for raw in self._iter_rows(sheet_ids):
            pnr = str(raw.get("RecordLocator") or "").strip()
            if not pnr or pnr.lower() in ("nan", "none", ""):
                continue

            amount = _safe_float(raw.get("PaymentAmount"))
            dc = str(raw.get("Debit or Credit") or "").strip().lower()

            if dc == "debit":
                agg[pnr]["sale"] += amount
            elif dc == "credit":
                agg[pnr]["refund"] += abs(amount)

            # Capture BookingDate (first non-null value wins)
            if agg[pnr]["booking_date"] is None:
                raw_date = raw.get("BookingDate") or raw.get("Booking Date") or raw.get("booking_date")
                booking_date = _safe_date(raw_date)
                if booking_date:
                    agg[pnr]["booking_date"] = booking_date

            # Capture customer/passenger name from Name1 (first non-null wins)
            if agg[pnr]["customer_name"] is None:
                name = raw.get("Name1") or raw.get("name1")
                if name and str(name).strip().lower() not in ("nan", "none", ""):
                    agg[pnr]["customer_name"] = str(name).strip()

        result = {}
        for pnr, v in agg.items():
            v["net"] = round(v["sale"] - v["refund"], 2)
            v["sale"]   = round(v["sale"], 2)
            v["refund"] = round(v["refund"], 2)
            result[pnr] = v

        logger.info("AIR COST TRN: %s unique PNRs aggregated.", len(result))
        return result

    def _aggregate_gross_fare(
        self, sheet_map: dict, sheet_key: str
    ) -> dict[str, float]:
        """
        Aggregate GROSS FARE by PNR for CASH x SAle or CASH X Re.

        PNR column:
          CASH x SAle  → "Formatted PNR"
          CASH X Re    → "PNR formatted"
        """
        sheet_ids = sheet_map.get(sheet_key)
        pnr_col = "Formatted PNR" if sheet_key == _SHEET_CASHX_SALE else "PNR formatted"

        agg: dict[str, float] = defaultdict(float)

        for raw in self._iter_rows(sheet_ids):
            pnr = str(raw.get(pnr_col) or "").strip()
            if not pnr or pnr.lower() in ("nan", "none", ""):
                continue
            agg[pnr] += _safe_float(raw.get("GROSS FARE"))

        result = {pnr: round(v, 2) for pnr, v in agg.items()}
        logger.info("%s: %s unique PNRs aggregated.", sheet_key, len(result))
        return result

    def _aggregate_spyj_sale(self, sheet_map: dict) -> dict[str, float]:
        """Aggregate SPYJ SALE: Total Amount by GDS PNR."""
        sheet_ids = sheet_map.get(_SHEET_SPYJ_SALE)
        agg: dict[str, float] = defaultdict(float)

        for raw in self._iter_rows(sheet_ids):
            pnr = str(raw.get("GDS PNR") or "").strip()
            if not pnr or pnr.lower() in ("nan", "none", ""):
                continue
            agg[pnr] += _safe_float(raw.get("Total Amount"))

        result = {pnr: round(v, 2) for pnr, v in agg.items()}
        logger.info("SPYJ SALE: %s unique PNRs aggregated.", len(result))
        return result

    def _aggregate_spyj_refund(self, sheet_map: dict) -> dict[str, float]:
        """Aggregate SPJY Refund: Total Refund Amount by GDS PNR."""
        sheet_ids = sheet_map.get(_SHEET_SPJY_REF)
        agg: dict[str, float] = defaultdict(float)

        for raw in self._iter_rows(sheet_ids):
            pnr = str(raw.get("GDS PNR") or "").strip()
            if not pnr or pnr.lower() in ("nan", "none", ""):
                continue
            agg[pnr] += _safe_float(raw.get("Total Refund Amount"))

        result = {pnr: round(v, 2) for pnr, v in agg.items()}
        logger.info("SPJY Refund: %s unique PNRs aggregated.", len(result))
        return result

    # ------------------------------------------------------------------ #
    # Merge helpers                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _merge_sale_refund(
        sale_agg: dict[str, float],
        refund_agg: dict[str, float],
    ) -> dict[str, dict]:
        """
        Combine separate sale and refund dicts (both keyed by PNR) into a
        unified dict with keys ``sale``, ``refund``, ``net``.

        PNRs present in only one of the two inputs are included with 0 for
        the missing side.
        """
        all_pnrs = set(sale_agg.keys()) | set(refund_agg.keys())
        result = {}
        for pnr in all_pnrs:
            sale   = sale_agg.get(pnr, 0.0)
            refund = refund_agg.get(pnr, 0.0)
            result[pnr] = {
                "sale":   round(sale, 2),
                "refund": round(refund, 2),
                "net":    round(sale - refund, 2),
            }
        return result

    # ------------------------------------------------------------------ #
    # Bulk insert                                                          #
    # ------------------------------------------------------------------ #

    def _bulk_insert(self, rows: list[dict]) -> int:
        """Insert *rows* into reconciliation_results in chunks.

        The ``_remarks`` key (list of remark labels) is stripped before
        insertion — it is only used by ``_bulk_insert_remarks``.
        """
        if not rows:
            return 0

        total = 0
        stmt = insert(ReconciliationResult)

        for start in range(0, len(rows), _CHUNK_SIZE):
            chunk = [
                {k: v for k, v in row.items() if k != "_remarks"}
                for row in rows[start : start + _CHUNK_SIZE]
            ]
            self._db.execute(stmt, chunk)
            total += len(chunk)
            logger.debug(
                "  Inserted reconciliation rows %s–%s.",
                start + 1,
                start + len(chunk),
            )

        return total

    def _bulk_insert_remarks(self, rows: list[dict]) -> None:
        """Insert one ReconciliationRemark row per label per result.

        Fetches the newly-inserted result IDs by PNR so we can reference them.
        """
        if not rows:
            return

        # Re-fetch the result IDs that were just inserted for this file
        result_file_id = rows[0]["uploaded_file_id"]
        pnr_to_id: dict[str, int] = {
            pnr: rid
            for pnr, rid in self._db.query(
                ReconciliationResult.pnr, ReconciliationResult.id
            )
            .filter(ReconciliationResult.uploaded_file_id == result_file_id)
            .all()
        }

        remark_rows: list[dict] = []
        for row in rows:
            result_id = pnr_to_id.get(row["pnr"])
            if result_id is None:
                continue
            for label in row.get("_remarks", []):
                remark_rows.append({"result_id": result_id, "remark": label})

        if not remark_rows:
            return

        stmt = insert(ReconciliationRemark)
        for start in range(0, len(remark_rows), _CHUNK_SIZE):
            chunk = remark_rows[start : start + _CHUNK_SIZE]
            self._db.execute(stmt, chunk)
            logger.debug(
                "  Inserted reconciliation remark rows %s–%s.",
                start + 1,
                start + len(chunk),
            )
