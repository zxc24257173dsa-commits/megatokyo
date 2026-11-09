from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.conftest import uid


def entry(db, etype, amount, *, d=date(2026, 11, 2), key=None, reversal_of=None):
    return db.execute(text("""
        INSERT INTO financial_entries (entry_date, occurred_at, entry_type, amount_orig, currency_orig, fx_rate,
                                       amount_base, source_type, idempotency_key, reversal_of_id)
        VALUES (:d, now(), :t, :a, 'USD', 1, :a, 'manual', :k, :r) RETURNING id"""),
        {"d": d, "t": etype, "a": amount, "k": key or uid("fe-"), "r": reversal_of}).scalar_one()


def test_profit_is_sum_of_signed_entries(db):
    keys = [uid("p-") for _ in range(4)]
    entry(db, "revenue", 100, key=keys[0])
    entry(db, "cogs", -40, key=keys[1])
    entry(db, "platform_fee", -8, key=keys[2])
    entry(db, "payment_processing_fee", -3.2, key=keys[3])
    total = db.execute(text("SELECT SUM(amount_base) FROM financial_entries WHERE idempotency_key = ANY(:k)"),
                       {"k": keys}).scalar_one()
    assert float(total) == pytest.approx(48.8)  # revenue is not profit


@pytest.mark.parametrize("etype,amount", [("platform_fee", 5), ("cogs", 10), ("refund", 1), ("revenue", -5)])
def test_wrong_sign_rejected(db, etype, amount):
    with pytest.raises(IntegrityError, match="ck_financial_entries_sign_by_type"):
        with db.begin_nested():
            entry(db, etype, amount)


def test_correction_by_reversal(db):
    orig = entry(db, "revenue", 50)
    entry(db, "revenue", -50, reversal_of=orig)
    with pytest.raises(IntegrityError, match="uq_financial_entries_reversal_of_id"):
        with db.begin_nested():
            entry(db, "revenue", -50, reversal_of=orig)  # cannot reverse twice


def test_entries_are_append_only(db):
    eid = entry(db, "revenue", 10)
    with pytest.raises(DBAPIError, match="ERP_APPEND_ONLY"):
        with db.begin_nested():
            db.execute(text("UPDATE financial_entries SET amount_base = 99 WHERE id = :i"), {"i": eid})
    with pytest.raises(DBAPIError, match="ERP_APPEND_ONLY"):
        with db.begin_nested():
            db.execute(text("DELETE FROM financial_entries WHERE id = :i"), {"i": eid})


def test_same_source_event_cannot_be_recorded_twice(db):
    entry(db, "revenue", 10, key="whatnot:order:123:line:1:revenue")
    with pytest.raises(IntegrityError, match="uq_financial_entries_idempotency_key"):
        with db.begin_nested():
            entry(db, "revenue", 10, key="whatnot:order:123:line:1:revenue")


def test_closed_period_rejects_new_entries(db):
    db.execute(text("INSERT INTO accounting_periods (period, status, closed_at) VALUES ('2026-09', 'closed', now())"))
    with pytest.raises(DBAPIError, match="ERP_PERIOD_CLOSED"):
        with db.begin_nested():
            entry(db, "revenue", 10, d=date(2026, 9, 15))
    entry(db, "revenue", 10, d=date(2026, 10, 1))  # open period still fine
