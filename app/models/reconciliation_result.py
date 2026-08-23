"""
ReconciliationResult Model
--------------------------
One row per unique PNR produced by the reconciliation engine.

Structure mirrors the original "Reconcilation" sheet in the client workbook:

  Cost side  (AIR COST TRN)   : cost_pnr, cost_sale, cost_refund, cost_net
  CASH X side (CASH x SAle /
               CASH X Re)     : cashx_pnr, cashx_amount, cashx_refund, cashx_net
  SPYJ side  (SPYJ SALE /
              SPJY Refund)    : spyj_pnr, spyj_amount, spyj_refund, spyj_net
  Variance                    : variance  (cost_net − cashx_net − spyj_net)
  Remarks                     : remark, revised_remark, final_remark
"""

from datetime import datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class ReconciliationResult(Base):
    """Stores one reconciled row per PNR for an uploaded file."""

    __tablename__ = "reconciliation_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    uploaded_file_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("uploaded_files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ── Parental / join key ───────────────────────────────────────────────
    pnr: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )

    # Booking date taken from AIR COST TRN (BookingDate column)
    booking_date: Mapped[datetime | None] = mapped_column(
        Date,
        nullable=True,
    )

    # Customer / passenger name taken from AIR COST TRN (Name1 column)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── Cost side (AIR COST TRN) ─────────────────────────────────────────
    # Matched PNR string (same as pnr, or "not found")
    cost_pnr: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cost_sale: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_refund: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_net: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # ── CASH X side (CASH x SAle − CASH X Re) ───────────────────────────
    cashx_pnr: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cashx_client_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cashx_client_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cashx_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cashx_refund: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cashx_net: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # ── SPYJ Online side (SPYJ SALE − SPJY Refund) ──────────────────────
    spyj_pnr: Mapped[str | None] = mapped_column(String(50), nullable=True)
    spyj_client_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    spyj_client_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    spyj_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    spyj_refund: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    spyj_net: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # ── Variance & remarks ────────────────────────────────────────────────
    # variance = cost_net − cashx_net − spyj_net
    variance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Auto-assigned remark based on variance magnitude / pattern
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Optional human-override fields (carried forward from the source sheet)
    revised_remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    final_remark: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── Audit ─────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    uploaded_file = relationship(
        "UploadedFile",
        backref="reconciliation_results",
        lazy="select",
    )

    # One result can have many individual remark rows
    remarks = relationship(
        "ReconciliationRemark",
        back_populates="result",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<ReconciliationResult("
            f"id={self.id}, "
            f"file_id={self.uploaded_file_id}, "
            f"pnr='{self.pnr}', "
            f"variance={self.variance})>"
        )
