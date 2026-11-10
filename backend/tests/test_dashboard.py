"""Phase 11: home dashboard - blocks follow the user's permissions; money only with finance access."""
from app.integrations.whatnot import processing as P
from tests import whatnot_fake as W
from tests.conftest import auth_header, token_for
from tests.test_catalog import EDITOR, create_product, skus_by_code
from tests.test_whatnot import us_stock


def test_dashboard_blocks_by_role(api, db):
    _, tok = token_for(api, db, *EDITOR)
    create_product(api, tok, codes=("DB-1",))
    us_stock(api, tok, skus_by_code(api, tok)["DB-1"]["id"], 5, "4")
    P.process_order(db, W.order("DB-O1", created="2026-11-01T20:00:00Z"),
                    [W.item("DB-I1", "DB-O1", ext="DB-1", product_id="DBP")], source="api")       # paid, not shipped
    P.process_order(db, W.order("DB-O2", created="2026-11-01T20:00:00Z"),
                    [W.item("DB-I2", "DB-O2", title="Mystery #4", product_id="X")], source="api")   # unmapped
    db.commit()

    _, mgr = token_for(api, db, "tw_manager")
    d = api.get("/api/v1/dashboard", headers=auth_header(mgr)).json()
    keys = {x["key"]: x for x in d["todo"]}
    assert keys["unmapped_products"]["count"] == 1 and keys["late_outbound"]["count"] == 2
    assert keys["replenishment_never_run"]["link"] == "/replenishment"
    assert "month" in d and "recent_shows" in d and d["this_week"]["sales"] is not None
    assert d["stock"]["available"] == 4 and "us_value" in d["stock"]

    _, inv = token_for(api, db, "jp_inventory")  # no finance, but can see stock cost (cost import role)
    d = api.get("/api/v1/dashboard", headers=auth_header(inv)).json()
    assert "month" not in d and "recent_shows" not in d and d["this_week"]["sales"] is None

    _, ro = token_for(api, db, "read_only")
    d = api.get("/api/v1/dashboard", headers=auth_header(ro)).json()
    assert "month" not in d and "us_value" not in d["stock"]
    assert "last_sync" not in d
