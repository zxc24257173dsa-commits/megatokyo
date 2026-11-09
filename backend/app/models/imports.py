"""Two-phase file import: upload -> parse -> validate -> preview -> (user confirms) -> commit.
Nothing reaches business tables until commit, which runs in a single transaction."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedMixin, IdMixin, TimestampMixin
from app.models._util import check_in

IMPORT_TYPES = ("purchase_cost", "whatnot_orders", "whatnot_livestream", "whatnot_payout", "expenses", "opening_stock")
IMPORT_STATUSES = ("uploaded", "parsed", "validated", "committed", "failed", "cancelled")


class ImportJob(IdMixin, TimestampMixin, Base):
    __tablename__ = "import_jobs"

    import_type: Mapped[str] = mapped_column(String(30), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    sheets: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    column_mapping: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="uploaded")
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    committed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        check_in("import_type", IMPORT_TYPES, "type_valid"),
        check_in("status", IMPORT_STATUSES, "status_valid"),
        # The same file can never be committed twice for the same purpose.
        Index(
            "uq_import_jobs_committed_file", "import_type", "file_sha256",
            unique=True, postgresql_where="status = 'committed'",
        ),
    )


class ImportRow(IdMixin, Base):
    __tablename__ = "import_rows"

    job_id: Mapped[int] = mapped_column(ForeignKey("import_jobs.id", ondelete="CASCADE"), nullable=False)
    sheet_name: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    normalized: Mapped[dict | None] = mapped_column(JSONB)
    matched_sku_id: Mapped[int | None] = mapped_column(ForeignKey("skus.id"))
    proposed_action: Mapped[str | None] = mapped_column(String(30))
    user_decision: Mapped[str | None] = mapped_column(String(30))
    errors: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")

    __table_args__ = (UniqueConstraint("job_id", "sheet_name", "row_number"),)


class ImportMappingTemplate(IdMixin, CreatedMixin, Base):
    """Remembers how a spreadsheet's headers map to our fields, keyed by a header signature."""

    __tablename__ = "import_mapping_templates"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    import_type: Mapped[str] = mapped_column(String(30), nullable=False)
    header_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (UniqueConstraint("import_type", "header_signature"),)
