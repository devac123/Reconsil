"""
ReconciliationRemark Model
--------------------------
One row per remark per reconciled PNR.

A single ReconciliationResult can have multiple remarks, e.g.
    "Not in SPYJ"  +  "Not in CASH X"

stored as two separate rows rather than a comma-joined string.
"""

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class ReconciliationRemark(Base):
    """Stores individual remark labels for a reconciled PNR row."""

    __tablename__ = "reconciliation_remarks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    result_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("reconciliation_results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # e.g. "Matched", "Not in SPYJ", "Not in CASH X", "Variance", etc.
    remark: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    result = relationship(
        "ReconciliationResult",
        back_populates="remarks",
    )

    def __repr__(self) -> str:
        return (
            f"<ReconciliationRemark("
            f"id={self.id}, "
            f"result_id={self.result_id}, "
            f"remark='{self.remark}')>"
        )
