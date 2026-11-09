import struct
import zlib

from tests.conftest import auth_header, idem, token_for

EDITOR = ("jp_inventory", "us_live_ops", "tw_manager")  # catalog.edit + both warehouses, no MFA needed


def png_bytes() -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = b"\x00\xff\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def create_product(api, tok, codes=("PK-001",), name="Pokemon Card Box", **extra):
    r = api.post("/api/v1/products", headers=auth_header(tok), json={
        "name": name, **extra,
        "skus": [{"sku_code": c, "variant_name": f"v{i}", "list_price": "49.99"} for i, c in enumerate(codes)]})
    assert r.status_code == 201, r.text
    return r.json()


def skus_by_code(api, tok, **params):
    return {s["sku_code"]: s for s in api.get("/api/v1/skus", headers=auth_header(tok), params=params).json()}


def test_create_product_with_variants_and_list(api, db):
    _, tok = token_for(api, db, *EDITOR)
    cat = api.post("/api/v1/categories", headers=auth_header(tok), json={"name": "Trading Cards"}).json()
    p = create_product(api, tok, codes=("PK-151-A", "PK-151-B"), category_id=cat["id"])
    assert len(p["sku_ids"]) == 2
    rows = skus_by_code(api, tok, search="pk-151")
    assert set(rows) == {"PK-151-A", "PK-151-B"}
    a = rows["PK-151-A"]
    assert a["category_name"] == "Trading Cards" and a["name"] == "Pokemon Card Box"
    assert (a["jp_on_hand"], a["in_transit"], a["us_available"]) == (0, 0, 0)


def test_sku_codes_unique_case_insensitive(api, db):
    _, tok = token_for(api, db, *EDITOR)
    create_product(api, tok, codes=("ABC-1",))
    r = api.post("/api/v1/products", headers=auth_header(tok),
                 json={"name": "x", "skus": [{"sku_code": "abc-1"}]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "sku_code_taken"
    r = api.post("/api/v1/products", headers=auth_header(tok),
                 json={"name": "x", "skus": [{"sku_code": "NEW-1"}, {"sku_code": "new-1"}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "duplicate_sku_code"


def test_update_sku_with_version_and_immutable_code(api, db):
    _, tok = token_for(api, db, *EDITOR)
    sid = create_product(api, tok, codes=("VER-1",))["sku_ids"][0]
    d = api.get(f"/api/v1/skus/{sid}", headers=auth_header(tok)).json()
    r = api.patch(f"/api/v1/skus/{sid}", headers=auth_header(tok),
                  json={"version": d["version"], "list_price": "59.00", "safety_stock_override": 5})
    assert r.status_code == 200 and r.json()["list_price"] == "59.0000" and r.json()["version"] == d["version"] + 1
    stale = api.patch(f"/api/v1/skus/{sid}", headers=auth_header(tok), json={"version": d["version"], "name": "x"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "modified_by_someone_else"
    code = api.patch(f"/api/v1/skus/{sid}", headers=auth_header(tok),
                     json={"version": d["version"] + 1, "sku_code": "OTHER"})
    assert code.status_code == 422


def test_read_only_can_view_but_not_edit(api, db):
    _, tok = token_for(api, db, "read_only")
    assert api.get("/api/v1/skus", headers=auth_header(tok)).status_code == 200
    r = api.post("/api/v1/products", headers=auth_header(tok), json={"name": "x", "skus": [{"sku_code": "RO-1"}]})
    assert r.status_code == 403


def test_bundle_availability_is_complete_sets(api, db):
    _, tok = token_for(api, db, *EDITOR)
    a, b = create_product(api, tok, codes=("BD-A", "BD-B"))["sku_ids"]
    for sku, qty in ((a, 7), (b, 2)):
        r = api.post("/api/v1/inventory/opening-stock", headers={**auth_header(tok), **idem()}, json={
            "location_code": "US_MAIN", "lines": [{"sku_id": sku, "qty": qty, "unit_cost_usd": "3"}]})
        assert r.status_code == 201, r.text
    r = api.post("/api/v1/bundles", headers=auth_header(tok), json={
        "sku_code": "BD-SET", "name": "Starter set", "list_price": "99",
        "components": [{"sku_id": a, "qty": 3}, {"sku_id": b, "qty": 1}]})
    assert r.status_code == 201, r.text
    bundle = r.json()
    assert bundle["is_bundle"] and {c["sku_code"] for c in bundle["components"]} == {"BD-A", "BD-B"}
    assert bundle["us_available"] == 2  # min(7 // 3, 2 // 1)
    comp = api.get(f"/api/v1/skus/{a}", headers=auth_header(tok)).json()
    assert comp["used_in_bundles"][0]["sku_code"] == "BD-SET"

    nested = api.post("/api/v1/bundles", headers=auth_header(tok), json={
        "sku_code": "BD-NEST", "name": "x", "components": [{"sku_id": bundle["id"], "qty": 1}]})
    assert nested.status_code == 422 and nested.json()["error"]["code"] == "nested_bundle"

    upd = api.put(f"/api/v1/skus/{bundle['id']}/components", headers=auth_header(tok),
                  json={"version": bundle["version"], "components": [{"sku_id": a, "qty": 2}]})
    assert upd.status_code == 200 and upd.json()["us_available"] == 3
    stale = api.put(f"/api/v1/skus/{bundle['id']}/components", headers=auth_header(tok),
                    json={"version": bundle["version"], "components": [{"sku_id": a, "qty": 1}]})
    assert stale.status_code == 409


def test_image_upload_signed_url_and_validation(api, db):
    _, tok = token_for(api, db, *EDITOR)
    sid = create_product(api, tok, codes=("IMG-1",))["sku_ids"][0]
    r = api.post(f"/api/v1/skus/{sid}/images", headers=auth_header(tok),
                 files={"file": ("photo.png", png_bytes(), "image/png")})
    assert r.status_code == 201, r.text
    img = r.json()
    assert img["is_primary"] and img["content_type"] == "image/png"
    got = api.get(img["url"])  # no auth header: the signature authorises
    assert got.status_code == 200 and got.content == png_bytes() and got.headers["content-type"] == "image/png"
    assert api.get(img["url"].replace("sig=", "sig=0")).status_code == 404

    fake = api.post(f"/api/v1/skus/{sid}/images", headers=auth_header(tok),
                    files={"file": ("evil.png", b"<script>alert(1)</script>", "image/png")})
    assert fake.status_code == 415

    second = api.post(f"/api/v1/skus/{sid}/images", headers=auth_header(tok),
                      files={"file": ("2.png", png_bytes(), "image/png")}).json()
    assert second["is_primary"] is False
    assert api.post(f"/api/v1/skus/{sid}/images/{second['id']}/primary", headers=auth_header(tok)).status_code == 204
    detail = api.get(f"/api/v1/skus/{sid}", headers=auth_header(tok)).json()
    assert [i["is_primary"] for i in detail["images"]] == [False, True]
    assert detail["image_url"] is not None
    assert api.delete(f"/api/v1/skus/{sid}/images/{second['id']}", headers=auth_header(tok)).status_code == 204
    detail = api.get(f"/api/v1/skus/{sid}", headers=auth_header(tok)).json()
    assert len(detail["images"]) == 1 and detail["images"][0]["is_primary"]


def test_cost_visible_only_with_permission(api, db):
    _, editor = token_for(api, db, *EDITOR)
    sid = create_product(api, editor, codes=("COST-1",))["sku_ids"][0]
    api.post("/api/v1/inventory/opening-stock", headers={**auth_header(editor), **idem()}, json={
        "location_code": "JP_MAIN", "lines": [{"sku_id": sid, "qty": 4, "unit_cost_usd": "12.5"}]})
    _, ops = token_for(api, db, "us_live_ops")  # no finance.view / import.cost
    d = api.get(f"/api/v1/skus/{sid}", headers=auth_header(ops)).json()
    assert d["can_view_cost"] is False and d["inventory_value_usd"] is None
    assert d["lots"][0]["unit_cost_base"] is None
    _, jp = token_for(api, db, "jp_inventory")  # import.cost
    d = api.get(f"/api/v1/skus/{sid}", headers=auth_header(jp)).json()
    assert d["can_view_cost"] and d["inventory_value_usd"] == "50.0000"
    assert d["lots"][0]["unit_cost_base"] == "12.500000"
