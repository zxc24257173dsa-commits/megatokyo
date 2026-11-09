from sqlalchemy import text


def perms(db, role):
    return set(db.execute(text("""
        SELECT p.code FROM role_permissions rp
        JOIN roles r ON r.id = rp.role_id JOIN permissions p ON p.id = rp.permission_id
        WHERE r.code = :r"""), {"r": role}).scalars())


def test_locations_seeded(db):
    rows = dict(db.execute(text("SELECT code, kind FROM locations")).all())
    assert rows == {
        "JP_MAIN": "physical", "US_MAIN": "physical", "IN_TRANSIT": "in_transit",
        "SUPPLIER": "virtual", "CUSTOMER": "virtual", "ADJUSTMENT": "virtual", "LOSS": "virtual",
    }


def test_currencies_seeded(db):
    assert set(db.execute(text("SELECT code FROM currencies")).scalars()) == {"USD", "JPY", "TWD"}


def test_role_permission_matrix(db):
    all_perms = set(db.execute(text("SELECT code FROM permissions")).scalars())
    assert perms(db, "admin") == all_perms
    assert "integration.manage" not in perms(db, "tw_manager")       # only admin touches API tokens
    assert "finance.view" not in perms(db, "us_live_ops")            # US ops sees livestream P&L only
    assert "finance.view_livestream" in perms(db, "us_live_ops")
    assert "shipment.manage_jp" in perms(db, "jp_inventory")
    assert "shipment.receive_us" not in perms(db, "jp_inventory")
    assert "purchase.approve" in perms(db, "tw_manager")
    assert "purchase.approve" not in perms(db, "jp_inventory")       # JP can edit suggestions, not approve
    ro = perms(db, "read_only")
    assert ro and all(p.endswith(".view") for p in ro)


def test_replenishment_defaults_match_owner_inputs(db):
    s = db.execute(text("SELECT * FROM replenishment_settings")).mappings().one()
    assert s["lead_time_days"] == 9
    assert s["review_period_days"] == 7
    assert s["supplier_lead_time_days"] == 11
    assert s["safety_stock_method"] == "volatility"
