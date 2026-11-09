"""Direct shipping: purchased goods go from the supplier in Japan straight to the US.

Owner (2026-11-09): goods are not stored in Japan. A purchase line is
  shipped from Japan  -> SUPPLIER   -> IN_TRANSIT  (purchase_receipt; creates the FIFO lot)
  received in the US  -> IN_TRANSIT -> US_MAIN     (transfer_receive)
SUPPLIER -> US_MAIN is also allowed for goods delivered without an in-transit step.
The JP_MAIN rules stay so a Japan warehouse can be switched on later.

Revision ID: 0008
Revises: 0007
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RULES = [("purchase_receipt", "SUPPLIER", "IN_TRANSIT"), ("purchase_receipt", "SUPPLIER", "US_MAIN")]


def upgrade() -> None:
    for t, f, to in RULES:
        op.execute(f"INSERT INTO movement_type_rules VALUES ('{t}', '{f}', '{to}') ON CONFLICT DO NOTHING")


def downgrade() -> None:
    for t, f, to in RULES:
        op.execute(f"DELETE FROM movement_type_rules WHERE movement_type = '{t}' AND from_location_code = '{f}' "
                   f"AND to_location_code = '{to}'")
