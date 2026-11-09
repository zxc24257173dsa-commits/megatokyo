"""Database-level integrity rules, inventory views and reference data.

These rules live in PostgreSQL (not only in application code) so that NO code path,
script or manual SQL can break them:

  * append-only ledgers (movements, financial entries, audit log, COGS, lots, ...)
  * inventory balances maintained automatically from movements; never negative;
    cannot be written directly
  * movement direction rules (e.g. sales only from US_MAIN; in-transit stock can't be sold)
  * bundles never hold stock; bundle components can't be bundles
  * purchase-batch lines frozen once confirmed (historical cost is never overwritten)
  * no new financial entries in a closed accounting period

Error messages start with ERP_<CODE>: so the API can map them to 409 responses.

Revision ID: 0002
Revises: 0001
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "inventory_movements",
    "inventory_lots",
    "lot_cost_adjustments",
    "financial_entries",
    "cogs_allocations",
    "audit_logs",
    "order_status_history",
    "shipment_events",
)

MOVEMENT_RULES = [
    ("purchase_receipt", "SUPPLIER", "JP_MAIN"),
    ("opening_balance", "ADJUSTMENT", "JP_MAIN"),
    ("opening_balance", "ADJUSTMENT", "US_MAIN"),
    ("opening_balance", "ADJUSTMENT", "IN_TRANSIT"),
    ("transfer_ship", "JP_MAIN", "IN_TRANSIT"),
    ("transfer_receive", "IN_TRANSIT", "US_MAIN"),
    ("transfer_loss", "IN_TRANSIT", "LOSS"),
    ("sale", "US_MAIN", "CUSTOMER"),
    ("customer_return", "CUSTOMER", "US_MAIN"),
    ("adjustment_in", "ADJUSTMENT", "JP_MAIN"),
    ("adjustment_in", "ADJUSTMENT", "US_MAIN"),
    ("adjustment_out", "JP_MAIN", "ADJUSTMENT"),
    ("adjustment_out", "US_MAIN", "ADJUSTMENT"),
    ("damage", "JP_MAIN", "LOSS"),
    ("damage", "US_MAIN", "LOSS"),
]

ROLES = [
    ("admin", "管理員", "Full access"),
    ("jp_inventory", "日本庫存管理員", "Japan warehouse, purchasing, JP->US shipments"),
    ("us_live_ops", "美國直播營運人員", "US warehouse receiving, livestream sales"),
    ("tw_manager", "台灣管理人員", "Management, finance, approvals"),
    ("read_only", "唯讀使用者", "View only"),
]

PERMISSIONS = {
    "users.manage": "Manage users and roles",
    "audit.view": "View audit log",
    "catalog.view": "View products and SKUs",
    "catalog.edit": "Create/edit products, SKUs, bundles",
    "inventory.view": "View stock levels and movements",
    "inventory.adjust_jp": "Receive/adjust stock in Japan warehouse",
    "inventory.adjust_us": "Receive/adjust stock in US warehouse",
    "stock_count.approve": "Approve stock count differences",
    "shipment.manage_jp": "Create, pack and ship JP->US shipments",
    "shipment.receive_us": "Receive JP->US shipments in the US",
    "import.cost": "Import purchase cost spreadsheets",
    "import.sales": "Import Whatnot order/livestream/payout files",
    "orders.view": "View orders and livestreams",
    "finance.view": "View full financial reports and dashboard",
    "finance.view_livestream": "View per-livestream P&L",
    "expense.create": "Record operating expenses",
    "period.close": "Close/reopen accounting periods",
    "replenishment.view": "View replenishment suggestions",
    "replenishment.edit": "Override replenishment suggestions",
    "purchase.approve": "Approve purchase orders",
    "integration.manage": "Manage Whatnot API connection and tokens",
}

ROLE_PERMISSIONS = {
    "admin": list(PERMISSIONS),
    "jp_inventory": [
        "catalog.view", "catalog.edit", "inventory.view", "inventory.adjust_jp", "shipment.manage_jp",
        "import.cost", "orders.view", "replenishment.view", "replenishment.edit",
    ],
    "us_live_ops": [
        "catalog.view", "inventory.view", "inventory.adjust_us", "shipment.receive_us", "import.sales",
        "orders.view", "finance.view_livestream", "expense.create", "replenishment.view",
    ],
    "tw_manager": [
        "catalog.view", "catalog.edit", "inventory.view", "stock_count.approve", "import.cost", "import.sales",
        "orders.view", "finance.view", "finance.view_livestream", "expense.create", "replenishment.view",
        "purchase.approve", "audit.view",
    ],
    "read_only": ["catalog.view", "inventory.view", "orders.view", "replenishment.view"],
}


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX ix_skus_search_trgm ON skus USING gin ((sku_code || ' ' || name) gin_trgm_ops)")

    # ---------- append-only ledgers ----------
    op.execute("""
    CREATE FUNCTION erp_forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'ERP_APPEND_ONLY: % is append-only; % is not allowed (use a reversal entry)',
            TG_TABLE_NAME, TG_OP USING ERRCODE = 'restrict_violation';
    END $$;
    """)
    for t in APPEND_ONLY_TABLES:
        op.execute(f"""
        CREATE TRIGGER trg_{t}_no_update_delete BEFORE UPDATE OR DELETE ON {t}
            FOR EACH ROW EXECUTE FUNCTION erp_forbid_mutation();
        CREATE TRIGGER trg_{t}_no_truncate BEFORE TRUNCATE ON {t}
            FOR EACH STATEMENT EXECUTE FUNCTION erp_forbid_mutation();
        """)

    # ---------- movements: direction rules, bundle guard, automatic balances ----------
    op.execute("""
    CREATE FUNCTION erp_movement_validate() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE v_from text; v_to text; v_bundle boolean; v_lot_sku bigint;
    BEGIN
        SELECT code INTO v_from FROM locations WHERE id = NEW.from_location_id;
        SELECT code INTO v_to   FROM locations WHERE id = NEW.to_location_id;
        IF NOT EXISTS (SELECT 1 FROM movement_type_rules
                       WHERE movement_type = NEW.movement_type
                         AND from_location_code = v_from AND to_location_code = v_to) THEN
            RAISE EXCEPTION 'ERP_MOVEMENT_DIRECTION: % cannot move stock from % to %',
                NEW.movement_type, v_from, v_to USING ERRCODE = 'check_violation';
        END IF;
        SELECT is_bundle INTO v_bundle FROM skus WHERE id = NEW.sku_id;
        IF v_bundle THEN
            RAISE EXCEPTION 'ERP_BUNDLE_NO_STOCK: bundle SKU % holds no stock; move its components',
                NEW.sku_id USING ERRCODE = 'check_violation';
        END IF;
        SELECT sku_id INTO v_lot_sku FROM inventory_lots WHERE id = NEW.lot_id;
        IF v_lot_sku IS DISTINCT FROM NEW.sku_id THEN
            RAISE EXCEPTION 'ERP_LOT_SKU_MISMATCH: lot % does not belong to sku %', NEW.lot_id, NEW.sku_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$;

    CREATE TRIGGER trg_inventory_movements_validate BEFORE INSERT ON inventory_movements
        FOR EACH ROW EXECUTE FUNCTION erp_movement_validate();

    CREATE FUNCTION erp_movement_apply_balance() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE v_from_kind text; v_to_kind text;
    BEGIN
        SELECT kind INTO v_from_kind FROM locations WHERE id = NEW.from_location_id;
        SELECT kind INTO v_to_kind   FROM locations WHERE id = NEW.to_location_id;

        IF v_from_kind <> 'virtual' THEN
            UPDATE inventory_balances SET qty = qty - NEW.qty, updated_at = now()
             WHERE sku_id = NEW.sku_id AND location_id = NEW.from_location_id AND lot_id = NEW.lot_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'ERP_INSUFFICIENT_STOCK: no stock of sku % lot % at location %',
                    NEW.sku_id, NEW.lot_id, NEW.from_location_id USING ERRCODE = 'check_violation';
            END IF;
        END IF;

        IF v_to_kind <> 'virtual' THEN
            INSERT INTO inventory_balances (sku_id, location_id, lot_id, qty, updated_at)
            VALUES (NEW.sku_id, NEW.to_location_id, NEW.lot_id, NEW.qty, now())
            ON CONFLICT (sku_id, location_id, lot_id)
            DO UPDATE SET qty = inventory_balances.qty + EXCLUDED.qty, updated_at = now();
        END IF;
        RETURN NULL;
    END $$;

    CREATE TRIGGER trg_inventory_movements_balance AFTER INSERT ON inventory_movements
        FOR EACH ROW EXECUTE FUNCTION erp_movement_apply_balance();
    """)
    # A negative result violates ck_inventory_balances_qty_nonneg -> the whole movement rolls back.

    op.execute("""
    CREATE FUNCTION erp_balance_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF pg_trigger_depth() <= 1 THEN
            RAISE EXCEPTION 'ERP_BALANCE_DIRECT_WRITE: inventory_balances is derived from inventory_movements; insert a movement instead'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$;

    CREATE TRIGGER trg_inventory_balances_guard BEFORE INSERT OR UPDATE OR DELETE ON inventory_balances
        FOR EACH ROW EXECUTE FUNCTION erp_balance_guard();
    CREATE TRIGGER trg_inventory_balances_no_truncate BEFORE TRUNCATE ON inventory_balances
        FOR EACH STATEMENT EXECUTE FUNCTION erp_forbid_mutation();
    """)

    # ---------- bundles ----------
    op.execute("""
    CREATE FUNCTION erp_bundle_component_validate() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT (SELECT is_bundle FROM skus WHERE id = NEW.bundle_sku_id) THEN
            RAISE EXCEPTION 'ERP_NOT_A_BUNDLE: sku % is not marked as a bundle', NEW.bundle_sku_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF (SELECT is_bundle FROM skus WHERE id = NEW.component_sku_id) THEN
            RAISE EXCEPTION 'ERP_NESTED_BUNDLE: component % is itself a bundle', NEW.component_sku_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$;

    CREATE TRIGGER trg_sku_bundle_components_validate BEFORE INSERT OR UPDATE ON sku_bundle_components
        FOR EACH ROW EXECUTE FUNCTION erp_bundle_component_validate();

    CREATE FUNCTION erp_lot_validate() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF (SELECT is_bundle FROM skus WHERE id = NEW.sku_id) THEN
            RAISE EXCEPTION 'ERP_BUNDLE_NO_STOCK: bundle SKU % cannot have lots', NEW.sku_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$;

    CREATE TRIGGER trg_inventory_lots_validate BEFORE INSERT ON inventory_lots
        FOR EACH ROW EXECUTE FUNCTION erp_lot_validate();
    """)

    # ---------- purchase batches: confirmed history is frozen ----------
    op.execute("""
    CREATE FUNCTION erp_purchase_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE v_status text;
    BEGIN
        SELECT status INTO v_status FROM purchase_batches
         WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.batch_id ELSE NEW.batch_id END;
        IF v_status <> 'draft' THEN
            RAISE EXCEPTION 'ERP_BATCH_LOCKED: purchase batch is % ; its cost lines cannot be changed', v_status
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF TG_OP = 'UPDATE' AND OLD.batch_id <> NEW.batch_id THEN
            SELECT status INTO v_status FROM purchase_batches WHERE id = OLD.batch_id;
            IF v_status <> 'draft' THEN
                RAISE EXCEPTION 'ERP_BATCH_LOCKED: cannot move a line out of a % batch', v_status
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$;

    CREATE TRIGGER trg_purchase_lines_guard BEFORE INSERT OR UPDATE OR DELETE ON purchase_lines
        FOR EACH ROW EXECUTE FUNCTION erp_purchase_line_guard();

    CREATE FUNCTION erp_purchase_batch_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            IF OLD.status <> 'draft' THEN
                RAISE EXCEPTION 'ERP_BATCH_LOCKED: a % purchase batch cannot be deleted', OLD.status
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN OLD;
        END IF;
        IF OLD.status <> 'draft' AND (
               NEW.status = 'draft'
            OR NEW.purchase_date IS DISTINCT FROM OLD.purchase_date
            OR NEW.batch_no IS DISTINCT FROM OLD.batch_no
            OR NEW.supplier_id IS DISTINCT FROM OLD.supplier_id) THEN
            RAISE EXCEPTION 'ERP_BATCH_LOCKED: a % purchase batch cannot be reopened or edited', OLD.status
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF OLD.status = 'locked' AND NEW.status <> 'locked' THEN
            RAISE EXCEPTION 'ERP_BATCH_LOCKED: a locked batch cannot change status'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END $$;

    CREATE TRIGGER trg_purchase_batches_guard BEFORE UPDATE OR DELETE ON purchase_batches
        FOR EACH ROW EXECUTE FUNCTION erp_purchase_batch_guard();
    """)

    # ---------- closed accounting periods ----------
    op.execute("""
    CREATE FUNCTION erp_period_open_check() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF EXISTS (SELECT 1 FROM accounting_periods
                   WHERE period = to_char(NEW.entry_date, 'YYYY-MM') AND status = 'closed') THEN
            RAISE EXCEPTION 'ERP_PERIOD_CLOSED: accounting period % is closed', to_char(NEW.entry_date, 'YYYY-MM')
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END $$;

    CREATE TRIGGER trg_financial_entries_period_open BEFORE INSERT ON financial_entries
        FOR EACH ROW EXECUTE FUNCTION erp_period_open_check();
    """)

    # ---------- views ----------
    op.execute("""
    CREATE VIEW v_inventory_position AS
    WITH bal AS (
        SELECT b.sku_id, l.code, SUM(b.qty) AS qty
          FROM inventory_balances b JOIN locations l ON l.id = b.location_id
         GROUP BY b.sku_id, l.code
    ), res AS (
        SELECT r.sku_id, SUM(r.qty) AS qty
          FROM inventory_reservations r JOIN locations l ON l.id = r.location_id
         WHERE r.status = 'reserved' AND l.code = 'US_MAIN'
         GROUP BY r.sku_id
    )
    SELECT s.id AS sku_id,
           s.sku_code,
           COALESCE(MAX(bal.qty) FILTER (WHERE bal.code = 'JP_MAIN'), 0)::int    AS jp_on_hand,
           COALESCE(MAX(bal.qty) FILTER (WHERE bal.code = 'IN_TRANSIT'), 0)::int AS in_transit,
           COALESCE(MAX(bal.qty) FILTER (WHERE bal.code = 'US_MAIN'), 0)::int    AS us_on_hand,
           COALESCE(MAX(res.qty), 0)::int                                         AS us_reserved,
           (COALESCE(MAX(bal.qty) FILTER (WHERE bal.code = 'US_MAIN'), 0)
            - COALESCE(MAX(res.qty), 0))::int                                     AS us_available
      FROM skus s
      LEFT JOIN bal ON bal.sku_id = s.id
      LEFT JOIN res ON res.sku_id = s.id
     WHERE NOT s.is_bundle
     GROUP BY s.id, s.sku_code;

    COMMENT ON VIEW v_inventory_position IS
      'us_available = US on-hand minus open reservations. in_transit is NEVER included in us_available.';

    CREATE VIEW v_inventory_reconciliation AS
    WITH ledger AS (
        SELECT sku_id, lot_id, location_id, SUM(delta) AS qty FROM (
            SELECT sku_id, lot_id, to_location_id AS location_id, qty AS delta FROM inventory_movements
            UNION ALL
            SELECT sku_id, lot_id, from_location_id, -qty FROM inventory_movements
        ) m GROUP BY sku_id, lot_id, location_id
    )
    SELECT COALESCE(l.sku_id, b.sku_id) AS sku_id,
           COALESCE(l.lot_id, b.lot_id) AS lot_id,
           COALESCE(l.location_id, b.location_id) AS location_id,
           COALESCE(l.qty, 0) AS ledger_qty,
           COALESCE(b.qty, 0) AS balance_qty
      FROM ledger l
      JOIN locations loc ON loc.id = l.location_id AND loc.kind <> 'virtual'
      FULL OUTER JOIN inventory_balances b
        ON b.sku_id = l.sku_id AND b.lot_id = l.lot_id AND b.location_id = l.location_id
     WHERE COALESCE(l.qty, 0) <> COALESCE(b.qty, 0);

    COMMENT ON VIEW v_inventory_reconciliation IS 'Rows here mean the balance cache disagrees with the ledger. Must be empty.';
    """)

    # ---------- reference data (configuration, not business data) ----------
    op.execute("""
    INSERT INTO currencies (code, name, minor_units) VALUES
        ('USD', 'US Dollar', 2), ('JPY', 'Japanese Yen', 0), ('TWD', 'New Taiwan Dollar', 2)
        ON CONFLICT DO NOTHING;

    INSERT INTO locations (code, name, kind, country, timezone) VALUES
        ('JP_MAIN',    '日本倉庫', 'physical',   'JP', 'Asia/Tokyo'),
        ('US_MAIN',    '美國倉庫', 'physical',   'US', 'America/New_York'),
        ('IN_TRANSIT', '運送中',   'in_transit', NULL, NULL),
        ('SUPPLIER',   '供應商',   'virtual',    NULL, NULL),
        ('CUSTOMER',   '顧客',     'virtual',    NULL, NULL),
        ('ADJUSTMENT', '盤點調整', 'virtual',    NULL, NULL),
        ('LOSS',       '遺失/損壞', 'virtual',   NULL, NULL)
        ON CONFLICT DO NOTHING;

    INSERT INTO replenishment_settings (id) VALUES (1) ON CONFLICT DO NOTHING;
    """)
    op.execute(
        "INSERT INTO movement_type_rules (movement_type, from_location_code, to_location_code) VALUES "
        + ", ".join(f"({_q(t)}, {_q(f)}, {_q(to)})" for t, f, to in MOVEMENT_RULES)
        + " ON CONFLICT DO NOTHING"
    )
    op.execute(
        "INSERT INTO roles (code, name, description) VALUES "
        + ", ".join(f"({_q(c)}, {_q(n)}, {_q(d)})" for c, n, d in ROLES)
        + " ON CONFLICT DO NOTHING"
    )
    op.execute(
        "INSERT INTO permissions (code, description) VALUES "
        + ", ".join(f"({_q(c)}, {_q(d)})" for c, d in PERMISSIONS.items())
        + " ON CONFLICT DO NOTHING"
    )
    for role, perms in ROLE_PERMISSIONS.items():
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            f"SELECT r.id, p.id FROM roles r, permissions p WHERE r.code = {_q(role)} "
            f"AND p.code IN ({', '.join(_q(p) for p in perms)}) ON CONFLICT DO NOTHING"
        )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_inventory_reconciliation")
    op.execute("DROP VIEW IF EXISTS v_inventory_position")
    for t in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_no_update_delete ON {t}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_no_truncate ON {t}")
    for trg, table in [
        ("trg_inventory_movements_validate", "inventory_movements"),
        ("trg_inventory_movements_balance", "inventory_movements"),
        ("trg_inventory_balances_guard", "inventory_balances"),
        ("trg_inventory_balances_no_truncate", "inventory_balances"),
        ("trg_sku_bundle_components_validate", "sku_bundle_components"),
        ("trg_inventory_lots_validate", "inventory_lots"),
        ("trg_purchase_lines_guard", "purchase_lines"),
        ("trg_purchase_batches_guard", "purchase_batches"),
        ("trg_financial_entries_period_open", "financial_entries"),
    ]:
        op.execute(f"DROP TRIGGER IF EXISTS {trg} ON {table}")
    for fn in [
        "erp_forbid_mutation", "erp_movement_validate", "erp_movement_apply_balance", "erp_balance_guard",
        "erp_bundle_component_validate", "erp_lot_validate", "erp_purchase_line_guard",
        "erp_purchase_batch_guard", "erp_period_open_check",
    ]:
        op.execute(f"DROP FUNCTION IF EXISTS {fn}()")
    # Reference data (locations, roles, currencies ...) is intentionally kept: business rows
    # reference it, and the seed inserts are idempotent so re-upgrading is safe.
    op.execute("DROP INDEX IF EXISTS ix_skus_search_trgm")
    # pg_trgm extension is left installed (harmless, may be shared).
