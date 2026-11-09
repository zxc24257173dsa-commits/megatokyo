"""Whatnot seller financial report -> real fees, refunds and adjustments in the ledger.

Source: POST /reports {report_type: seller_financial_report} -> asynchronous run -> CSV (one row per
ledger transaction). Verified on the owner's account (2026-11-09, 7 days, 1,194 rows): exactly one
ORDER_EARNINGS row per order, and for every row

    transaction_amount = post_coupon_price - commission_fee - commission_tax
                         - processing_fee - processing_fee_tax - seller_shipping_charge

What is booked (financial_entries, idempotency key `whatnot:ledger:<ledger_transaction_id>:<part>`):

  ORDER_EARNINGS                 platform_fee (commission + its tax), payment_processing_fee (+ tax),
                                 shipping_cost (seller-paid shipping). NOT revenue: revenue comes from the
                                 order itself (Phase 5), so nothing is counted twice.
  ORDER_REFUND                   refund. A full refund (= minus the order's earnings) is split into
                                 refund of the item price + reversal of that order's fees; anything else is
                                 booked as the net amount Whatnot took back.
  ORDER_REFUND_SHIPPING_COSTS,   shipping_cost (or shipping_adjustment when money comes back)
  SHIPPING_CHARGE, CARRIER_ADJUSTMENT(_REFUND), APPEALED_ORDER_REFUND_SHIPPING_COSTS
  APPEALED_ORDER_REFUND          refund_adjustment (refund won back on appeal)
  TIP                            other_income

ORDER_EARNINGS for an order the ERP does not have (e.g. sold before go-live) is stored but not booked
(`order_not_in_erp`); it is retried on every sync. Unknown types / non-USD -> `needs_review`.

Privacy: buyer_name, buyer_state, buyer_country and listing_description are never read or stored.
`cost_of_goods` is ignored (cost comes from FIFO purchase lots).
"""
import csv
import io
import logging
import tempfile
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.integrations.whatnot import processing as P
from app.models.finance import FinancialEntry, PlatformReportRun, PlatformTransaction
from app.models.sales import SalesOrder, SalesOrderLine

log = logging.getLogger("app.whatnot.financial")
REPORT_TYPE = "seller_financial_report"
MAX_REPORT_BYTES = 200 * 1024 * 1024
ZERO = Decimal(0)

COLUMNS = (
    "transaction_completed_at_utc", "order_placed_at_utc", "transaction_type", "transaction_message", "order_id",
    "listing_title", "buy_format", "sale_type", "quantity_sold", "sku", "livestream_id", "shipment_id",
    "transaction_currency", "transaction_amount", "buyer_paid", "original_item_price", "coupon_cost",
    "post_coupon_price", "seller_shipping_charge", "commission_fee", "commission_tax", "processing_fee",
    "processing_fee_tax", "ledger_transaction_id",
)
PRIVATE_COLUMNS = ("buyer_name", "buyer_state", "buyer_country")
MONEY = ("amount", "buyer_paid", "original_item_price", "coupon_cost", "post_coupon_price", "seller_shipping_charge",
         "commission_fee", "commission_tax", "processing_fee", "processing_fee_tax")
FROZEN = ("transaction_type", "currency", "completed_at") + MONEY[:1] + MONEY[3:]

SHIPPING_TYPES = ("ORDER_REFUND_SHIPPING_COSTS", "SHIPPING_CHARGE", "CARRIER_ADJUSTMENT",
                  "CARRIER_ADJUSTMENT_REFUND", "APPEALED_ORDER_REFUND_SHIPPING_COSTS")
KNOWN_TYPES = ("ORDER_EARNINGS", "ORDER_REFUND", "APPEALED_ORDER_REFUND", "TIP") + SHIPPING_TYPES
TYPE_LABEL = {
    "ORDER_EARNINGS": "訂單收入", "ORDER_REFUND": "退款", "ORDER_REFUND_SHIPPING_COSTS": "退款運費",
    "APPEALED_ORDER_REFUND": "申訴退回的退款", "APPEALED_ORDER_REFUND_SHIPPING_COSTS": "申訴退回的運費",
    "SHIPPING_CHARGE": "運費扣款", "CARRIER_ADJUSTMENT": "物流商調整", "CARRIER_ADJUSTMENT_REFUND": "物流商調整退回",
    "TIP": "小費",
}


class RowError(ValueError):
    pass


# ---------- parsing (shared by the API report and an uploaded CSV) ----------

def _blank(v) -> str | None:
    s = "" if v is None else str(v).strip()
    return s or None


def _money(v, field: str) -> Decimal | None:
    s = _blank(v)
    if s is None:
        return None
    try:
        return Decimal(s.replace(",", "").replace("$", ""))
    except InvalidOperation:
        raise RowError(f"{field}：不是數字（{s[:20]}）") from None


def _ts(v) -> datetime | None:
    if isinstance(v, datetime):
        dt = v if v.tzinfo else v.replace(tzinfo=UTC)
    else:
        s = _blank(v)
        if s is None:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            raise RowError(f"時間格式無法辨識（{s[:30]}）") from None
        dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # the report is in UTC
    return None if dt.year <= 1970 else dt  # Whatnot writes 1970-01-01 for "no order"


def _id(v) -> str | None:
    s = _blank(v)
    return None if s in (None, "0") else s  # shipment_id "0" = none


def parse_row(row: dict) -> dict:
    """Report row (column name -> text) to normalized values. Raises RowError."""
    g = row.get
    ext = _blank(g("ledger_transaction_id"))
    if not ext:
        raise RowError("缺少 ledger_transaction_id")
    ttype = (_blank(g("transaction_type")) or "").upper()
    if not ttype:
        raise RowError("缺少 transaction_type")
    completed = _ts(g("transaction_completed_at_utc"))
    if completed is None:
        raise RowError("缺少交易完成時間")
    amount = _money(g("transaction_amount"), "transaction_amount")
    if amount is None:
        raise RowError("缺少 transaction_amount")
    qty = _blank(g("quantity_sold"))
    try:
        qty_i = int(Decimal(qty)) if qty else None
    except InvalidOperation:
        raise RowError("quantity_sold 不是數字") from None
    out = {
        "external_id": ext, "transaction_type": ttype, "completed_at": completed,
        "order_placed_at": _ts(g("order_placed_at_utc")), "external_order_id": _id(g("order_id")),
        "external_livestream_id": _id(g("livestream_id")), "external_shipment_id": _id(g("shipment_id")),
        "currency": (_blank(g("transaction_currency")) or "USD").upper()[:3], "amount": amount,
        "quantity_sold": qty_i, "buy_format": (_blank(g("buy_format")) or "")[:40] or None,
        "sale_type": (_blank(g("sale_type")) or "")[:40] or None,
        "listing_title": (_blank(g("listing_title")) or "")[:500] or None,
        "sku_text": (_blank(g("sku")) or "")[:128] or None,
        "message": (_blank(g("transaction_message")) or "")[:1000] or None,
    }
    for f in MONEY[1:]:
        out[f] = _money(g(f), f)
    return out


# ---------- storing + booking ----------

def _same(stored: PlatformTransaction, n: dict) -> bool:
    for f in FROZEN:
        a, b = getattr(stored, f), n.get(f)
        if isinstance(a, Decimal) or isinstance(b, Decimal):
            if (a is None) != (b is None) or (a is not None and Decimal(a) != Decimal(b)):
                return False
        elif a != b:
            return False
    return True


def store(db: Session, n: dict, *, source: str, report_run_id: int | None = None,
          import_job_id: int | None = None) -> tuple[PlatformTransaction, str]:
    """Insert (or find) the transaction and book it. Returns (row, outcome)."""
    pt = db.scalar(select(PlatformTransaction).where(PlatformTransaction.platform == P.PLATFORM,
                                                     PlatformTransaction.external_id == n["external_id"])
                   .with_for_update())
    if pt is not None:
        if not _same(pt, n) and not pt.upstream_changed:
            pt.upstream_changed = True
            pt.detail = ((pt.detail or "") + " ／Whatnot 之後的報表對這筆交易給了不同的金額，已保留原本的數字，請人工確認").strip(" ／")
        if pt.status != "booked":
            book(db, pt)
            if pt.status == "booked":
                return pt, "booked_now"
        return pt, "existing"
    pt = PlatformTransaction(platform=P.PLATFORM, source=source, report_run_id=report_run_id,
                             import_job_id=import_job_id, status="needs_review", **n)
    db.add(pt)
    db.flush()
    book(db, pt)
    return pt, "new"


def _closed(db: Session, d) -> bool:
    return bool(db.scalar(text("SELECT 1 FROM accounting_periods WHERE period = :p AND status = 'closed'"),
                          {"p": d.strftime("%Y-%m")}))


def _when(db: Session, pt: PlatformTransaction, so: SalesOrder | None) -> datetime:
    """Fees belong to the order's date (same period as its revenue); if that month is already
    closed, they go to the date Whatnot completed the transaction (or today)."""
    for cand in ((so.ordered_at if so else None), pt.completed_at, datetime.now(UTC)):
        if cand is not None and not _closed(db, P.biz_date(cand)):
            return cand
    return datetime.now(UTC)


def _post(db: Session, pt: PlatformTransaction, part: str, entry_type: str, amount: Decimal, when: datetime,
          so: SalesOrder | None, livestream_id: int | None, line_id: int | None = None, memo: str | None = None,
          reversal_of: FinancialEntry | None = None) -> None:
    if amount == 0:
        return
    key = f"whatnot:ledger:{pt.external_id}:{part}"
    if db.scalar(select(FinancialEntry.id).where(FinancialEntry.idempotency_key == key)):
        return
    db.add(FinancialEntry(
        idempotency_key=key, entry_date=P.biz_date(when), occurred_at=when, entry_type=entry_type,
        amount_orig=P.q4(amount), currency_orig="USD", fx_rate=Decimal(1), amount_base=P.q4(amount),
        sales_order_id=so.id if so else None, order_line_id=line_id, livestream_id=livestream_id,
        sku_id=(db.get(SalesOrderLine, line_id).sku_id if line_id else None),
        source_type="api" if pt.source == "api" else "csv", source_ref=f"whatnot:ledger:{pt.external_id}",
        reversal_of_id=reversal_of.id if reversal_of else None, memo=memo))
    db.flush()


def _order(db: Session, pt: PlatformTransaction) -> SalesOrder | None:
    if pt.sales_order_id:
        return db.get(SalesOrder, pt.sales_order_id)
    if not pt.external_order_id:
        return None
    return db.scalar(select(SalesOrder).where(SalesOrder.platform == P.PLATFORM,
                                              SalesOrder.external_order_id == pt.external_order_id))


def _single_line(db: Session, so: SalesOrder) -> int | None:
    ids = list(db.scalars(select(SalesOrderLine.id).where(SalesOrderLine.order_id == so.id).limit(2)))
    return ids[0] if len(ids) == 1 else None


def _review(pt: PlatformTransaction, why: str) -> None:
    pt.status, pt.detail = "needs_review", why


def book(db: Session, pt: PlatformTransaction) -> None:
    so = _order(db, pt)
    if so is not None:
        pt.sales_order_id = so.id
    if pt.livestream_id is None:
        ls = P.livestream_for(db, pt.external_livestream_id, "api") if pt.external_livestream_id else None
        pt.livestream_id = (ls.id if ls else None) or (so.livestream_id if so else None)
    livestream_id = (so.livestream_id if so and so.livestream_id else None) or pt.livestream_id
    if pt.currency != "USD":
        return _review(pt, f"非 USD 交易（{pt.currency}），目前只處理 USD")
    t, amt = pt.transaction_type, pt.amount

    if t == "ORDER_EARNINGS":
        if so is None:
            pt.status, pt.detail = "order_not_in_erp", "ERP 沒有這筆訂單（可能是系統上線前的訂單），手續費暫不入帳；訂單同步進來後會自動補上"
            return
        fees = {"commission": ("platform_fee", (pt.commission_fee or ZERO) + (pt.commission_tax or ZERO)),
                "processing": ("payment_processing_fee", (pt.processing_fee or ZERO) + (pt.processing_fee_tax or ZERO)),
                "seller_shipping": ("shipping_cost", pt.seller_shipping_charge or ZERO)}
        if any(v < 0 for _, v in fees.values()):
            return _review(pt, "手續費出現負數（平台退回手續費？），請人工確認")
        when, line = _when(db, pt, so), _single_line(db, so)
        for part, (etype, v) in fees.items():
            _post(db, pt, part, etype, -v, when, so, livestream_id, line)
        pt.detail = _check_amount(db, pt, so)
    elif t == "ORDER_REFUND":
        if amt > 0:
            return _review(pt, "退款金額是正數，請人工確認")
        when = _when(db, pt, None)
        earn = None
        if pt.external_order_id:
            earn = db.scalar(select(PlatformTransaction).where(
                PlatformTransaction.external_order_id == pt.external_order_id,
                PlatformTransaction.transaction_type == "ORDER_EARNINGS", PlatformTransaction.status == "booked"))
        line = _single_line(db, so) if so else None
        if earn is not None and amt == -earn.amount and earn.post_coupon_price is not None:
            # full refund: the item price is refunded and the order's fees come back
            _post(db, pt, "refund", "refund", -earn.post_coupon_price, when, so, livestream_id, line,
                  memo="全額退款：退回商品金額")
            for part in ("commission", "processing", "seller_shipping"):
                orig = db.scalar(select(FinancialEntry).where(
                    FinancialEntry.idempotency_key == f"whatnot:ledger:{earn.external_id}:{part}"))
                if orig is not None and not db.scalar(select(FinancialEntry.id).where(FinancialEntry.reversal_of_id == orig.id)):
                    _post(db, pt, f"{part}_reversal", orig.entry_type, -orig.amount_base, when, so, livestream_id,
                          orig.order_line_id, memo="全額退款：平台退回手續費", reversal_of=orig)
        else:
            _post(db, pt, "refund", "refund", amt, when, so, livestream_id, line,
                  memo="退款（Whatnot 扣回的淨額）")
    elif t in SHIPPING_TYPES:
        _post(db, pt, "shipping", "shipping_cost" if amt <= 0 else "shipping_adjustment", amt, _when(db, pt, so),
              so, livestream_id, memo=TYPE_LABEL.get(t))
    elif t == "APPEALED_ORDER_REFUND":
        _post(db, pt, "refund_adjustment", "refund_adjustment", amt, _when(db, pt, so), so, livestream_id,
              memo="申訴成功，退款退回")
    elif t == "TIP":
        if amt < 0:
            return _review(pt, "小費金額是負數，請人工確認")
        _post(db, pt, "tip", "other_income", amt, _when(db, pt, so), so, livestream_id, memo="小費")
    else:
        return _review(pt, f"未知的交易類型 {t}，沒有入帳")
    pt.status, pt.booked_at = "booked", pt.booked_at or datetime.now(UTC)
    if t != "ORDER_EARNINGS":
        pt.detail = None
    db.flush()


def _check_amount(db: Session, pt: PlatformTransaction, so: SalesOrder) -> str | None:
    """Our booked item revenue for the order vs what Whatnot says the item sold for."""
    if pt.post_coupon_price is None or so.status not in P.PAID:
        return None
    lines = db.scalar(select(func.coalesce(func.sum(SalesOrderLine.line_amount), 0))
                      .where(SalesOrderLine.order_id == so.id))
    ours = Decimal(lines) - (so.discount_total or ZERO)
    if abs(ours - pt.post_coupon_price) > Decimal("0.01"):
        return f"金額不符：ERP 訂單 ${ours:.2f}，Whatnot 財務報表 ${pt.post_coupon_price:.2f}"
    return None


def retry_unbooked(db: Session, limit: int = 2000) -> int:
    n = 0
    for pt in db.scalars(select(PlatformTransaction).where(PlatformTransaction.status == "order_not_in_erp")
                         .order_by(PlatformTransaction.id).limit(limit)):
        book(db, pt)
        n += pt.status == "booked"
    return n


def ingest_rows(db: Session, rows, *, source: str, report_run_id: int | None = None,
                import_job_id: int | None = None) -> dict:
    stats = {"rows": 0, "new": 0, "existing": 0, "booked": 0, "order_not_in_erp": 0, "needs_review": 0,
             "errors": 0, "amount_mismatch": 0}
    for raw in rows:
        stats["rows"] += 1
        try:
            n = parse_row(raw)
        except RowError:
            stats["errors"] += 1
            continue
        pt, outcome = store(db, n, source=source, report_run_id=report_run_id, import_job_id=import_job_id)
        stats["new" if outcome == "new" else "existing"] += 1
        if outcome != "existing":  # statuses describe what THIS run changed
            stats[pt.status] += 1
        stats["amount_mismatch"] += bool(pt.detail and pt.detail.startswith("金額不符"))
    return stats


# ---------- report runs (API) ----------

def _fmt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:00:00.000Z")


def request_reports(db: Session, client, days: int, chunk_days: int = 7) -> list[PlatformReportRun]:
    """Ask Whatnot for the last `days` days, in chunks. Hour-aligned windows, so a repeated sync
    within the hour gets Whatnot's existing run back instead of a new one."""
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    runs = []
    start = end - timedelta(days=days)
    while start < end:
        stop = min(start + timedelta(days=chunk_days), end)
        r = client.request("POST", "/reports", json={"report_parameters": {
            "report_type": REPORT_TYPE, "start_date": _fmt(start), "end_date": _fmt(stop)}})
        runs.append(upsert_run(db, r))
        start = stop
    return runs


def upsert_run(db: Session, r: dict) -> PlatformReportRun:
    run = db.scalar(select(PlatformReportRun).where(PlatformReportRun.platform == P.PLATFORM,
                                                    PlatformReportRun.external_id == r["id"]))
    if run is None:
        run = PlatformReportRun(platform=P.PLATFORM, external_id=r["id"], report_type=r["report_type"],
                                parameters=r.get("report_parameters") or {}, status=r["status"])
        db.add(run)
    run.status = r["status"]
    db.flush()
    return run


def download_rows(url: str, transport: httpx.BaseTransport | None = None):
    """The report_url is a short-lived presigned link: fetched WITHOUT our API token, never logged."""
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as tmp:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), transport=transport) as http:
            with http.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"report download failed: HTTP {resp.status_code}")
                size = 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > MAX_REPORT_BYTES:
                        raise RuntimeError("report file too large")
                    tmp.write(chunk)
        tmp.seek(0)
        text_io = io.TextIOWrapper(tmp, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(text_io)
        missing = [c for c in ("ledger_transaction_id", "transaction_type", "transaction_amount")
                   if c not in (reader.fieldnames or [])]
        if missing:
            raise RuntimeError(f"unexpected report columns, missing {missing}")
        # drop private columns right away; only the fields in COLUMNS are ever looked at
        rows = [{k: r.get(k) for k in COLUMNS} for r in reader]
        text_io.detach()
    return rows


def process_runs(db: Session, client, runs: list[PlatformReportRun], *, wait_seconds: float = 120,
                 poll_every: float = 3.0) -> dict:
    """Poll the runs until done (or `wait_seconds`); runs still generating are picked up next sync."""
    transport, sleep = getattr(client, "transport", None), getattr(client, "_sleep", time.sleep)
    total: dict = {}
    polls_left = max(1, int(wait_seconds / poll_every))
    todo = [r for r in runs if r.processed_at is None]
    while todo:
        still = []
        for run in todo:
            r = client.get(f"/reports/{run.external_id}")
            run.status = r["status"]
            if r["status"] == "completed" and r.get("report_url"):
                try:
                    rows = download_rows(r["report_url"], transport)
                    stats = ingest_rows(db, rows, source="api", report_run_id=run.id)
                    run.stats, run.processed_at, run.error = stats, datetime.now(UTC), None
                    for k, v in stats.items():
                        total[k] = total.get(k, 0) + v
                except Exception as exc:  # keep the run; the next sync tries again
                    db.rollback()
                    run = db.get(PlatformReportRun, run.id)
                    run.error = str(exc)[:500]
                    log.exception("financial report processing failed")
                    total["failed_runs"] = total.get("failed_runs", 0) + 1
                db.commit()
            elif r["status"] in ("failed", "cancelled"):
                run.processed_at, run.error = datetime.now(UTC), f"Whatnot report {r['status']}"
                db.commit()
            else:
                still.append(run)
        todo = still
        if todo:
            polls_left -= 1
            if polls_left <= 0:
                total["still_generating"] = len(todo)
                db.commit()
                break
            sleep(poll_every)
    return total


def pending_runs(db: Session) -> list[PlatformReportRun]:
    """Runs from earlier syncs that were still generating."""
    since = datetime.now(UTC) - timedelta(days=3)
    return list(db.scalars(select(PlatformReportRun).where(
        PlatformReportRun.report_type == REPORT_TYPE, PlatformReportRun.processed_at.is_(None),
        PlatformReportRun.created_at >= since)))


def sync(db: Session, client, stats: dict, days: int, **kw) -> None:
    runs = request_reports(db, client, days)
    db.commit()
    seen = {r.id for r in runs}
    runs += [r for r in pending_runs(db) if r.id not in seen]
    res = process_runs(db, client, runs, **kw)
    stats["financial"] = res
    stats["financial_retried"] = retry_unbooked(db)
    db.commit()
