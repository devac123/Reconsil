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
from difflib import SequenceMatcher
import re
from typing import Callable

from sqlalchemy import insert, text
from sqlalchemy.orm import Session

from app.models.reconciliation_result import ReconciliationResult
from app.models.reconciliation_remark import ReconciliationRemark
from app.models.uploaded_file import UploadedFile
from app.models.uploaded_sheet import UploadedSheet
from app.models.staging_record import StagingRecord

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 500
_MAX_TICKET_NUMBER_LEN = 500
ProgressCallback = Callable[[int, str], None]

# ---------------------------------------------------------------------------
# Sheet-name constants (normalised)
# ---------------------------------------------------------------------------
_SHEET_AIR_COST   = "air cost trn"
_SHEET_CASHX_SALE = "cash x sale"
_SHEET_CASHX_RE   = "cash x re"
_SHEET_SPYJ_SALE  = "spyj sale"
_SHEET_SPJY_REF   = "spjy refund"

_SHEET_IGNORE = "ignore"
_SHEET_ROLE_KEYS = {
    _SHEET_AIR_COST,
    _SHEET_CASHX_SALE,
    _SHEET_CASHX_RE,
    _SHEET_SPYJ_SALE,
    _SHEET_SPJY_REF,
}

_SHEET_MATCH_THRESHOLD = 0.80

# ---------------------------------------------------------------------------
# Remark assignment thresholds
# ---------------------------------------------------------------------------
_MARKUP_VALUE   = 300.0
_MARKUP_TOL     = 10.0   # ±10 around 300 → "Markup/Booking Charges"
_MATCH_TOL      = 1.0    # |variance| < 1 → "Matched"

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "cost_pnr": ("RecordLocator", "Record Locator", "PNR", "GDS PNR"),
    "cost_amount": ("PaymentAmount", "Payment Amount", "Amount", "Total Amount"),
    "cost_debit_credit": ("Debit or Credit", "Debit/Credit", "Dr Cr", "DR/CR"),
    "booking_date": ("BookingDate", "Booking Date", "booking_date"),
    "booking_id": (
        "Booking ID",
        "BookingID",
        "Booking Id",
        "BOOKING ID",
        "BOOKINGID",
        "booking_id",
        "Booking Number",
        "BookingNumber",
        "RecordLocator",
        "GDS_recordlocator",
    ),
    "customer_name": (
        "Name1",
        "name1",
        "Client Name",
        "CLIENT NAME",
        "Customer Name",
        "CUSTOMER NAME",
        "Passenger Name",
        "PASSENGER NAME",
        "Pax Name",
        "PAX NAME",
        "PAX",
        "Name",
        "NAME",
    ),
    "client_name": (
        "Client Name",
        "CLIENT NAME",
        "Customer Name",
        "CUSTOMER NAME",
        "Passenger Name",
        "PASSENGER NAME",
        "Pax Name",
        "PAX NAME",
        "PAX",
        "Name",
        "NAME",
        "Name1",
        "name1",
    ),
    "client_code": ("Client Code", "CLIENT CODE", "Customer Code", "CUSTOMER CODE"),
    "cashx_sale_pnr": ("Formatted PNR", "PNR", "GDS PNR", "RecordLocator"),
    "cashx_refund_pnr": ("PNR formatted", "Formatted PNR", "PNR", "GDS PNR", "RecordLocator"),
    "gross_fare": ("GROSS FARE", "Gross Fare", "GrossFare", "Amount", "Total Amount"),
    "ticket_number": ("TKT NO", "Ticket No", "Ticket Number", "TicketNumbers", "Ticket Numbers"),
    "spyj_pnr": ("GDS PNR", "PNR", "Formatted PNR", "RecordLocator"),
    "spyj_sale_amount": (
        "SPYJ Amt",
        "SPYJ Amount",
        "SPYJAmt",
        "Total Amount",
        "Amount",
        "GROSS FARE",
        "Gross Fare",
    ),
    "spyj_refund_amount": ("Total Refund Amount", "Refund Amount", "Total Amount", "Amount"),
}

_ROLE_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    _SHEET_AIR_COST: ("cost_pnr", "cost_debit_credit"),
    _SHEET_CASHX_SALE: ("cashx_sale_pnr",),
    _SHEET_CASHX_RE: ("cashx_refund_pnr",),
    _SHEET_SPYJ_SALE: ("spyj_pnr",),
    _SHEET_SPJY_REF: ("spyj_pnr",),
}


def _normalise_name(value: str | None) -> str:
    """Strip spaces, lowercase, and remove symbols for flexible matching."""
    return re.sub(r"[^a-z0-9]", "", (value or "").strip().lower())


def _should_skip_sheet(sheet_name: str) -> bool:
    normalised = _normalise_name(sheet_name)
    return any(
        marker in normalised
        for marker in (
            "recon",
            "reconsil",
            "reconsilation",
            "reconciliation",
            "reconcilation",
        )
    )


def _match_score(left: str, right: str) -> float:
    left_key = _normalise_name(left)
    right_key = _normalise_name(right)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    return SequenceMatcher(None, left_key, right_key).ratio()


def _sheet_tokens(sheet_name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", sheet_name.lower()))


def _sheet_token_key(sheet_name: str) -> str:
    return _normalise_name(sheet_name)


def _canonical_sheet_name(sheet_name: str, context_name: str | None = None) -> str:
    """Map similar sheet names to the logical reconciliation sheet key."""
    tokens = _sheet_tokens(sheet_name)
    context_tokens = _sheet_tokens(context_name or "")
    all_tokens = tokens | context_tokens
    token_key = _sheet_token_key(sheet_name)
    context_key = _sheet_token_key(context_name or "")
    all_key = token_key + context_key

    if "cashx" in all_key or {"cash", "x"}.issubset(all_tokens):
        if tokens & {"refund", "ref", "re"}:
            return _SHEET_CASHX_RE
        if tokens & {"sale", "sales"}:
            return _SHEET_CASHX_SALE
        if context_tokens & {"refund", "ref", "re"}:
            return _SHEET_CASHX_RE
        if context_tokens & {"sale", "sales"}:
            return _SHEET_CASHX_SALE

    if tokens & {"spyj", "spjy"} or context_tokens & {"spyj", "spjy"} or "online" in all_tokens:
        if tokens & {"refund", "fund", "ref"}:
            return _SHEET_SPJY_REF
        if tokens & {"sale", "sales"}:
            return _SHEET_SPYJ_SALE
        if context_tokens & {"refund", "fund", "ref"} and not (context_tokens & {"sale", "sales"}):
            return _SHEET_SPJY_REF
        if context_tokens & {"sale", "sales"} and not (context_tokens & {"refund", "fund", "ref"}):
            return _SHEET_SPYJ_SALE

    if "cost" in all_key and not (tokens & {"recon", "reconciliation", "reconcilation"}):
        return _SHEET_AIR_COST

    best_name = sheet_name.strip().lower()
    best_score = 0.0
    for known_name in (
        _SHEET_AIR_COST,
        _SHEET_CASHX_SALE,
        _SHEET_CASHX_RE,
        _SHEET_SPYJ_SALE,
        _SHEET_SPJY_REF,
    ):
        score = _match_score(sheet_name, known_name)
        if score > best_score:
            best_name = known_name
            best_score = score
    return best_name if best_score >= _SHEET_MATCH_THRESHOLD else sheet_name.strip().lower()


def _normalise_sheet_role(role: str | None) -> str:
    role_key = (role or "").strip().lower()
    if not role_key or role_key == _SHEET_IGNORE:
        return _SHEET_IGNORE
    if role_key not in _SHEET_ROLE_KEYS:
        raise ValueError(
            "Invalid sheet role. Allowed roles are: "
            f"{sorted(_SHEET_ROLE_KEYS | {_SHEET_IGNORE})}"
        )
    return role_key


def _field_value(raw: dict, field_name: str):
    """Read a value using exact, normalised, then alias matching."""
    aliases = _FIELD_ALIASES.get(field_name, (field_name,))
    for key in aliases:
        value = raw.get(key)
        if _clean_text(value) is not None:
            return value

    raw_key_map = {_normalise_name(str(key)): key for key in raw.keys()}
    for alias in aliases:
        key = raw_key_map.get(_normalise_name(alias))
        if key is None:
            continue
        value = raw.get(key)
        if _clean_text(value) is not None:
            return value
    return None


def _raw_has_field_alias(raw: dict, field_name: str) -> bool:
    """Return True when *raw* contains any known alias for logical field."""
    raw_keys = {_normalise_name(str(key)) for key in raw.keys()}
    return any(
        _normalise_name(alias) in raw_keys
        for alias in _FIELD_ALIASES.get(field_name, (field_name,))
    )


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


def _clean_text(value) -> str | None:
    """Return a stripped display string, ignoring null-like spreadsheet text."""
    if value is None:
        return None
    text_value = str(value).strip()
    if not text_value or text_value.lower() in ("nan", "none", "nat"):
        return None
    return text_value


def _clean_ticket_number(value) -> str | None:
    """Return ticket text sized for the indexed staging column."""
    text_value = _clean_text(value)
    if text_value and len(text_value) > _MAX_TICKET_NUMBER_LEN:
        return text_value[:_MAX_TICKET_NUMBER_LEN]
    return text_value


def _first_text(raw: dict, *keys: str) -> str | None:
    """Return the first non-empty text value for any matching raw-data key."""
    for key in keys:
        value = _clean_text(_field_value(raw, key))
        if value:
            return value
    return None


def _extract_client_name(raw: dict) -> str | None:
    """Resolve client/passenger name from common CASH X and SPYJ headers."""
    return _clean_text(_field_value(raw, "client_name"))


def _extract_client_code(raw: dict) -> str | None:
    """Resolve client code from common CASH X and SPYJ headers."""
    return _clean_text(_field_value(raw, "client_code"))


def _extract_booking_id(raw: dict) -> str | None:
    """Resolve booking ID from common AIR COST TRN headers."""
    return _clean_text(_field_value(raw, "booking_id"))


def _normalize_ticket_number(value) -> str | None:
    """Return only ticket-number digits so cross-sheet joins survive suffixes."""
    text_value = _clean_text(value)
    if not text_value:
        return None
    text_value = re.sub(r"C\d+$", "", text_value, flags=re.IGNORECASE)
    digits = re.sub(r"\D", "", text_value)
    return digits or None


def _ticket_lookup_keys(value) -> list[str]:
    """Return plausible ticket keys, including 10-digit core ticket numbers."""
    ticket = _normalize_ticket_number(value)
    if not ticket:
        return []
    keys = [ticket]
    if len(ticket) >= 13:
        keys.append(ticket[3:13])
    if len(ticket) > 10:
        keys.append(ticket[-10:])
    return list(dict.fromkeys(k for k in keys if k))


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

    def reconcile(
        self,
        uploaded_file_id: int,
        progress: ProgressCallback | None = None,
    ) -> int:
        """
        Run reconciliation for *uploaded_file_id*.

        Deletes any existing results for this file, recomputes, and inserts
        fresh rows.

        Returns
        -------
        int
            Number of reconciled PNR rows produced.
        """
        return self.reconcile_combined(
            [uploaded_file_id],
            result_uploaded_file_id=uploaded_file_id,
            progress=progress,
        )

    def reconcile_combined(
        self,
        uploaded_file_ids: list[int],
        result_uploaded_file_id: int | None = None,
        selected_sheet_ids: list[int] | None = None,
        sheet_role_map: dict[int, str] | None = None,
        column_map: dict[int, dict[str, str]] | None = None,
        progress: ProgressCallback | None = None,
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

        selected_sheet_ids = (
            list(dict.fromkeys(selected_sheet_ids))
            if selected_sheet_ids is not None
            else None
        )
        if selected_sheet_ids is not None and not selected_sheet_ids:
            raise ValueError("At least one selected_sheet_id is required.")

        normalised_role_map: dict[int, str] | None = None
        if sheet_role_map is not None:
            normalised_role_map = {
                int(sheet_id): _normalise_sheet_role(role)
                for sheet_id, role in sheet_role_map.items()
            }
            selected_sheet_ids = [
                sheet_id
                for sheet_id, role in normalised_role_map.items()
                if role != _SHEET_IGNORE
            ]
            if not selected_sheet_ids:
                raise ValueError("Map at least one sheet before reconciliation.")

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
            "Starting combined reconciliation for uploaded_file_ids=%s; "
            "result_file_id=%s; selected_sheet_ids=%s; sheet_role_map=%s.",
            uploaded_file_ids,
            result_file_id,
            selected_sheet_ids,
            normalised_role_map,
        )
        self._emit_progress(progress, 3, "Preparing reconciliation.")

        # Load sheet-id map: normalised_sheet_name → list[sheet_id]
        self._emit_progress(progress, 8, "Mapping source sheets.")
        sheet_map = self._load_sheet_map(
            uploaded_file_ids,
            selected_sheet_ids,
            normalised_role_map,
        )
        logger.info("Sheet map: %s", {k: v for k, v in sheet_map.items()})

        normalised_column_map = self._normalise_column_map(column_map)
        if normalised_column_map:
            self._emit_progress(progress, 10, "Applying column mappings.")
            self._apply_column_map(normalised_column_map)

        self._emit_progress(progress, 12, "Validating required columns.")
        self._validate_required_columns(sheet_map)

        # Remove stale results from a previous run only after validation passes.
        self._emit_progress(progress, 15, "Clearing old reconciliation results.")
        self._db.execute(
            text(
                "DELETE FROM reconciliation_results "
                "WHERE uploaded_file_id = :fid"
            ),
            {"fid": result_file_id},
        )

        # Build aggregates per data source
        self._emit_progress(progress, 18, "Aggregating AIR COST rows.")
        cost_agg   = self._aggregate_cost(sheet_map)
        self._emit_progress(progress, 30, "Aggregating CASH X sale rows.")
        cashx_sale = self._aggregate_gross_fare(sheet_map, _SHEET_CASHX_SALE)
        self._emit_progress(progress, 40, "Aggregating CASH X refund rows.")
        cashx_re   = self._aggregate_gross_fare(sheet_map, _SHEET_CASHX_RE)
        self._emit_progress(progress, 48, "Building CASH X ticket lookup.")
        cashx_client_by_ticket = self._build_cashx_client_by_ticket(sheet_map)
        self._emit_progress(progress, 58, "Aggregating SPYJ sale rows.")
        spyj_sale  = self._aggregate_spyj_sale(sheet_map, cashx_client_by_ticket)
        self._emit_progress(progress, 68, "Aggregating SPYJ refund rows.")
        spyj_ref   = self._aggregate_spyj_refund(sheet_map, cashx_client_by_ticket)

        # CASH X net = sale - refund  (keyed by PNR)
        self._emit_progress(progress, 74, "Calculating net values.")
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
        self._emit_progress(progress, 80, f"Building {len(all_pnrs)} reconciliation rows.")
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
            cashx_client_name = (
                x["client_name"] if x and x.get("client_name") else
                c["customer_name"] if c and c.get("customer_name") else
                None
            )
            cashx_client_code = x["client_code"] if x and x.get("client_code") else None

            spyj_amount = s["sale"]   if s else 0.0
            spyj_refund = s["refund"] if s else 0.0
            spyj_net    = s["net"]    if s else 0.0
            spyj_client_name = (
                s["client_name"] if s and s.get("client_name") else
                cashx_client_name if cashx_client_name else
                c["customer_name"] if c and c.get("customer_name") else
                None
            )
            spyj_client_code = (
                s["client_code"] if s and s.get("client_code") else
                cashx_client_code if cashx_client_code else
                None
            )

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
                "booking_id":       c["booking_id"] if c else None,
                "customer_name":    c["customer_name"] if c else None,

                "cost_pnr":    pnr if c else "not found",
                "cost_sale":   cost_sale,
                "cost_refund": cost_refund,
                "cost_net":    cost_net,

                "cashx_pnr":         pnr if x else "not found",
                "cashx_client_name": cashx_client_name if x else None,
                "cashx_client_code": cashx_client_code if x else None,
                "cashx_amount":      cashx_amount,
                "cashx_refund":      cashx_refund,
                "cashx_net":         cashx_net,

                "spyj_pnr":         pnr if s else "not found",
                "spyj_client_name": spyj_client_name if s and spyj_client_name else "not found" if s else None,
                "spyj_client_code": spyj_client_code if s and spyj_client_code else "not found" if s else None,
                "spyj_amount":      spyj_amount,
                "spyj_refund":      spyj_refund,
                "spyj_net":         spyj_net,

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
        self._emit_progress(progress, 88, "Saving reconciliation rows.")
        total_inserted = self._bulk_insert(rows)

        # Now insert individual remark rows for every result
        self._emit_progress(progress, 94, "Saving reconciliation remarks.")
        self._bulk_insert_remarks(rows)

        self._db.commit()
        self._emit_progress(progress, 100, "Reconciliation complete.")
        logger.info(
            "Combined reconciliation complete for file_ids=%s: %s rows produced.",
            uploaded_file_ids,
            total_inserted,
        )
        return total_inserted

    def _normalise_column_map(
        self,
        column_map: dict[int, dict[str, str]] | None,
    ) -> dict[int, dict[str, str]]:
        """Keep only non-empty field-to-uploaded-column mappings."""
        if not column_map:
            return {}

        allowed_fields = set(_FIELD_ALIASES)
        normalised: dict[int, dict[str, str]] = {}
        for sheet_id, fields in column_map.items():
            try:
                parsed_sheet_id = int(sheet_id)
            except (TypeError, ValueError):
                continue
            if not isinstance(fields, dict):
                continue
            clean_fields = {
                str(field): str(column).strip()
                for field, column in fields.items()
                if field in allowed_fields and str(column or "").strip()
            }
            if clean_fields:
                normalised[parsed_sheet_id] = clean_fields
        return normalised

    def _apply_column_map(self, column_map: dict[int, dict[str, str]]) -> None:
        """
        Copy user-selected source columns into canonical raw-data keys.

        Original uploaded columns are preserved. The reconciliation formulas
        continue to read their existing aliases, but renamed headers now land
        under those canonical names before validation and aggregation.
        """
        if not column_map:
            return

        pnr_fields = {"cost_pnr", "cashx_sale_pnr", "cashx_refund_pnr", "spyj_pnr"}
        amount_fields = {
            "cost_amount",
            "gross_fare",
            "spyj_sale_amount",
            "spyj_refund_amount",
        }
        updated_rows = 0

        for sheet_id, fields in column_map.items():
            records = (
                self._db.query(StagingRecord)
                .filter(StagingRecord.uploaded_sheet_id == sheet_id)
                .yield_per(_CHUNK_SIZE)
            )
            for record in records:
                raw = dict(record.raw_data or {})
                changed = False

                for field, source_column in fields.items():
                    if source_column not in raw:
                        continue

                    target_column = _FIELD_ALIASES[field][0]
                    value = raw.get(source_column)
                    if field in amount_fields and _clean_text(value) is None:
                        value = 0
                    if raw.get(target_column) != value:
                        raw[target_column] = value
                        changed = True

                    if field in pnr_fields:
                        record.pnr = _clean_text(value)
                    elif field == "ticket_number":
                        record.ticket_number = _clean_ticket_number(value)

                if changed:
                    record.raw_data = raw
                    record.updated_at = datetime.utcnow()
                    updated_rows += 1

        if updated_rows:
            self._db.commit()
            logger.info("Applied column mapping to %s staged row(s).", updated_rows)

    @staticmethod
    def _emit_progress(progress: ProgressCallback | None, percent: int, message: str) -> None:
        if progress:
            progress(percent, message)

    # ------------------------------------------------------------------ #
    # Sheet loading helpers                                                #
    # ------------------------------------------------------------------ #

    def _load_sheet_map(
        self,
        uploaded_file_ids: list[int],
        selected_sheet_ids: list[int] | None = None,
        sheet_role_map: dict[int, str] | None = None,
    ) -> dict[str, list[int]]:
        """
        Return normalised_sheet_name → sheet_id list for uploaded workbooks.
        """
        filter_sheet_ids = (
            list(sheet_role_map.keys())
            if sheet_role_map is not None
            else selected_sheet_ids
        )
        query = self._db.query(UploadedSheet).filter(
            UploadedSheet.uploaded_file_id.in_(uploaded_file_ids)
        )
        if filter_sheet_ids is not None:
            query = query.filter(UploadedSheet.id.in_(filter_sheet_ids))

        sheets = query.order_by(
            UploadedSheet.uploaded_file_id,
            UploadedSheet.sheet_index,
        ).all()

        if filter_sheet_ids is not None:
            found_sheet_ids = {sheet.id for sheet in sheets}
            missing_sheet_ids = [
                sheet_id
                for sheet_id in filter_sheet_ids
                if sheet_id not in found_sheet_ids
            ]
            if missing_sheet_ids:
                raise ValueError(
                    "Selected sheet id(s) not found for uploaded files: "
                    f"{missing_sheet_ids}"
                )

        if not sheets:
            raise ValueError("No sheets are available for reconciliation.")

        sheet_map: dict[str, list[int]] = defaultdict(list)
        for sheet in sheets:
            mapped_role = sheet_role_map.get(sheet.id) if sheet_role_map else None
            if mapped_role == _SHEET_IGNORE:
                logger.info("Ignoring sheet '%s' by user mapping.", sheet.sheet_name)
                continue
            if mapped_role:
                sheet_map[mapped_role].append(sheet.id)
                continue
            if _should_skip_sheet(sheet.sheet_name):
                logger.info("Skipping reconciliation/result sheet '%s'.", sheet.sheet_name)
                continue
            context_name = ""
            if sheet.uploaded_file:
                context_name = " ".join(
                    str(value or "")
                    for value in (
                        sheet.uploaded_file.original_filename,
                        sheet.uploaded_file.stored_filename,
                        sheet.uploaded_file.file_path,
                    )
                )
            sheet_map[_canonical_sheet_name(sheet.sheet_name, context_name)].append(sheet.id)
        return dict(sheet_map)

    def _iter_rows(self, sheet_ids: list[int] | None):
        """
        Yield raw_data dicts for every staging record in *sheet_ids*.
        Streams rows without OFFSET pagination so large mapped datasets do not
        get slower as reconciliation advances through each sheet.
        """
        if not sheet_ids:
            return

        query = (
            self._db.query(StagingRecord.raw_data)
            .filter(StagingRecord.uploaded_sheet_id.in_(sheet_ids))
            .order_by(StagingRecord.uploaded_sheet_id, StagingRecord.row_number)
            .yield_per(_CHUNK_SIZE)
        )
        for (raw_data,) in query:
            yield raw_data

    def _validate_required_columns(self, sheet_map: dict[str, list[int]]) -> None:
        """
        Ensure every mapped source sheet has the columns needed for calculation.

        This catches renamed headers before deleting old results or producing
        misleading "not found" reconciliation rows.
        """
        problems: list[str] = []

        for role, required_fields in _ROLE_REQUIRED_FIELDS.items():
            sheet_ids = sheet_map.get(role)
            if not sheet_ids:
                continue

            sample_row = (
                self._db.query(StagingRecord.raw_data, UploadedSheet.sheet_name)
                .join(UploadedSheet, UploadedSheet.id == StagingRecord.uploaded_sheet_id)
                .filter(StagingRecord.uploaded_sheet_id.in_(sheet_ids))
                .order_by(StagingRecord.uploaded_sheet_id, StagingRecord.row_number)
                .first()
            )
            if not sample_row:
                problems.append(f"{role}: no imported rows found.")
                continue

            raw_data, sheet_name = sample_row
            missing_fields = [
                field
                for field in required_fields
                if not _raw_has_field_alias(raw_data or {}, field)
            ]
            if not missing_fields:
                continue

            details = []
            for field in missing_fields:
                aliases = " / ".join(_FIELD_ALIASES.get(field, (field,)))
                details.append(f"{field} [{aliases}]")
            problems.append(
                f"{sheet_name}: missing required column(s): {', '.join(details)}"
            )

        if problems:
            raise ValueError(
                "Column validation failed. Please check sheet headers or add aliases. "
                + " | ".join(problems)
            )

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
            lambda: {
                "sale": 0.0,
                "refund": 0.0,
                "booking_date": None,
                "booking_id": None,
                "customer_name": None,
            }
        )

        for raw in self._iter_rows(sheet_ids):
            pnr = str(_field_value(raw, "cost_pnr") or "").strip()
            if not pnr or pnr.lower() in ("nan", "none", ""):
                continue

            amount = _safe_float(_field_value(raw, "cost_amount"))
            dc = str(_field_value(raw, "cost_debit_credit") or "").strip().lower()

            if dc == "debit":
                agg[pnr]["sale"] += amount
            elif dc == "credit":
                agg[pnr]["refund"] += abs(amount)

            # Capture BookingDate (first non-null value wins)
            if agg[pnr]["booking_date"] is None:
                raw_date = _field_value(raw, "booking_date")
                booking_date = _safe_date(raw_date)
                if booking_date:
                    agg[pnr]["booking_date"] = booking_date

            # Capture Booking ID (first non-null value wins)
            if agg[pnr]["booking_id"] is None:
                booking_id = _extract_booking_id(raw)
                if booking_id:
                    agg[pnr]["booking_id"] = booking_id

            # Capture customer/passenger name from Name1 (first non-null wins)
            if agg[pnr]["customer_name"] is None:
                name = _clean_text(_field_value(raw, "customer_name"))
                if name:
                    agg[pnr]["customer_name"] = name

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
    ) -> dict[str, dict]:
        """
        Aggregate GROSS FARE by PNR for CASH x SAle or CASH X Re.

        PNR column:
          CASH x SAle  → "Formatted PNR"
          CASH X Re    → "PNR formatted"
        """
        sheet_ids = sheet_map.get(sheet_key)
        pnr_field = "cashx_sale_pnr" if sheet_key == _SHEET_CASHX_SALE else "cashx_refund_pnr"

        agg: dict[str, dict] = defaultdict(
            lambda: {"amount": 0.0, "client_name": None, "client_code": None}
        )

        for raw in self._iter_rows(sheet_ids):
            pnr = str(_field_value(raw, pnr_field) or "").strip()
            if not pnr or pnr.lower() in ("nan", "none", ""):
                continue
            agg[pnr]["amount"] += _safe_float(_field_value(raw, "gross_fare"))
            if agg[pnr]["client_name"] is None:
                agg[pnr]["client_name"] = _extract_client_name(raw)
            if agg[pnr]["client_code"] is None:
                agg[pnr]["client_code"] = _extract_client_code(raw)

        result = {
            pnr: {
                "amount": round(v["amount"], 2),
                "client_name": v["client_name"],
                "client_code": v["client_code"],
            }
            for pnr, v in agg.items()
        }
        logger.info("%s: %s unique PNRs aggregated.", sheet_key, len(result))
        return result

    def _build_cashx_client_by_ticket(self, sheet_map: dict) -> dict[str, dict]:
        """Build ticket-number → client lookup from CASH X sale/refund rows."""
        result: dict[str, dict] = {}
        for sheet_key in (_SHEET_CASHX_SALE, _SHEET_CASHX_RE):
            sheet_ids = sheet_map.get(sheet_key)
            for raw in self._iter_rows(sheet_ids):
                ticket = _normalize_ticket_number(_field_value(raw, "ticket_number"))
                if not ticket or ticket in result:
                    continue
                client_name = _extract_client_name(raw)
                client_code = _extract_client_code(raw)
                if client_name or client_code:
                    for key in _ticket_lookup_keys(_field_value(raw, "ticket_number")):
                        result.setdefault(
                            key,
                            {"client_name": client_name, "client_code": client_code},
                        )

        logger.info("CASH X: %s ticket client mappings built.", len(result))
        return result

    def _aggregate_spyj_sale(
        self,
        sheet_map: dict,
        cashx_client_by_ticket: dict[str, dict] | None = None,
    ) -> dict[str, dict]:
        """Aggregate SPYJ SALE: Total Amount by GDS PNR."""
        sheet_ids = sheet_map.get(_SHEET_SPYJ_SALE)
        agg: dict[str, dict] = defaultdict(
            lambda: {"amount": 0.0, "client_name": None, "client_code": None}
        )
        client_lookup = cashx_client_by_ticket or {}

        for raw in self._iter_rows(sheet_ids):
            pnr = str(_field_value(raw, "spyj_pnr") or "").strip()
            if not pnr or pnr.lower() in ("nan", "none", ""):
                continue
            agg[pnr]["amount"] += _safe_float(_field_value(raw, "spyj_sale_amount"))
            if agg[pnr]["client_name"] is None:
                agg[pnr]["client_name"] = _extract_client_name(raw)
            if agg[pnr]["client_code"] is None:
                agg[pnr]["client_code"] = _extract_client_code(raw)
            if agg[pnr]["client_name"] is None or agg[pnr]["client_code"] is None:
                for ticket in _ticket_lookup_keys(_field_value(raw, "ticket_number")):
                    client = client_lookup.get(ticket)
                    if not client:
                        continue
                    agg[pnr]["client_name"] = agg[pnr]["client_name"] or client.get("client_name")
                    agg[pnr]["client_code"] = agg[pnr]["client_code"] or client.get("client_code")
                    if agg[pnr]["client_name"] and agg[pnr]["client_code"]:
                        break

        result = {
            pnr: {
                "amount": round(v["amount"], 2),
                "client_name": v["client_name"],
                "client_code": v["client_code"],
            }
            for pnr, v in agg.items()
        }
        logger.info("SPYJ SALE: %s unique PNRs aggregated.", len(result))
        return result

    def _aggregate_spyj_refund(
        self,
        sheet_map: dict,
        cashx_client_by_ticket: dict[str, dict] | None = None,
    ) -> dict[str, dict]:
        """Aggregate SPJY Refund: Total Refund Amount by GDS PNR."""
        sheet_ids = sheet_map.get(_SHEET_SPJY_REF)
        agg: dict[str, dict] = defaultdict(
            lambda: {"amount": 0.0, "client_name": None, "client_code": None}
        )
        client_lookup = cashx_client_by_ticket or {}

        for raw in self._iter_rows(sheet_ids):
            pnr = str(_field_value(raw, "spyj_pnr") or "").strip()
            if not pnr or pnr.lower() in ("nan", "none", ""):
                continue
            agg[pnr]["amount"] += _safe_float(_field_value(raw, "spyj_refund_amount"))
            if agg[pnr]["client_name"] is None:
                agg[pnr]["client_name"] = _extract_client_name(raw)
            if agg[pnr]["client_code"] is None:
                agg[pnr]["client_code"] = _extract_client_code(raw)
            if agg[pnr]["client_name"] is None or agg[pnr]["client_code"] is None:
                for ticket in _ticket_lookup_keys(_field_value(raw, "ticket_number")):
                    client = client_lookup.get(ticket)
                    if not client:
                        continue
                    agg[pnr]["client_name"] = agg[pnr]["client_name"] or client.get("client_name")
                    agg[pnr]["client_code"] = agg[pnr]["client_code"] or client.get("client_code")
                    if agg[pnr]["client_name"] and agg[pnr]["client_code"]:
                        break

        result = {
            pnr: {
                "amount": round(v["amount"], 2),
                "client_name": v["client_name"],
                "client_code": v["client_code"],
            }
            for pnr, v in agg.items()
        }
        logger.info("SPJY Refund: %s unique PNRs aggregated.", len(result))
        return result

    # ------------------------------------------------------------------ #
    # Merge helpers                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _merge_sale_refund(
        sale_agg: dict[str, dict],
        refund_agg: dict[str, dict],
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
            sale_row = sale_agg.get(pnr, {})
            refund_row = refund_agg.get(pnr, {})
            sale = sale_row.get("amount", 0.0) if isinstance(sale_row, dict) else sale_row
            refund = refund_row.get("amount", 0.0) if isinstance(refund_row, dict) else refund_row
            sale_client = sale_row.get("client_name") if isinstance(sale_row, dict) else None
            refund_client = refund_row.get("client_name") if isinstance(refund_row, dict) else None
            sale_client_code = sale_row.get("client_code") if isinstance(sale_row, dict) else None
            refund_client_code = refund_row.get("client_code") if isinstance(refund_row, dict) else None
            result[pnr] = {
                "sale":        round(sale, 2),
                "refund":      round(refund, 2),
                "net":         round(sale - refund, 2),
                "client_name": sale_client or refund_client,
                "client_code": sale_client_code or refund_client_code,
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
