"""订单接口用例。数据在 tests/data/order.yaml。"""
from __future__ import annotations

import pytest

from tests.api_client import ApiClient
from tests.assertions import assert_order_status, assert_status, assert_stock
from tests.data_loader import cases


def create_order(api: ApiClient, token: str, items: list[dict]):
    return api.post("/api/orders", json={"items": items}, token=token)


class TestCreateOrder:
    def test_single_item_success(self, api: ApiClient, fresh_user: dict, fresh_product: dict, db_conn):
        pid = fresh_product["id"]

        r = create_order(api, fresh_user["token"], [{"product_id": pid, "qty": 2}])
        assert_status(r, 201)

        body = r.json()
        assert body["status"] == "created"
        assert body["total_cents"] == 20000
        assert_order_status(db_conn, body["order_id"], "created")
        assert_stock(db_conn, pid, fresh_product["stock"] - 2)

    def test_multi_item_amount(self, api: ApiClient, fresh_user: dict, fresh_product: dict):
        r = create_order(
            api,
            fresh_user["token"],
            [{"product_id": 1, "qty": 1}, {"product_id": fresh_product["id"], "qty": 3}],
        )
        assert_status(r, 201)
        assert r.json()["total_cents"] == 29900 + 10000 * 3

    @pytest.mark.parametrize("case", cases("order", "amount_precision"), ids=lambda c: c["id"])
    def test_amount_precision(self, api: ApiClient, fresh_user: dict, make_product, case: dict):
        p = make_product(price_cents=case["price_cents"], stock=10, prefix="零头商品")
        r = create_order(api, fresh_user["token"], [{"product_id": p["id"], "qty": case["qty"]}])
        assert_status(r, 201)
        assert r.json()["total_cents"] == case["expected"], f"{case['desc']}：期望 {case['expected']} 分"

    def test_stock_not_enough(self, api: ApiClient, fresh_user: dict, fresh_product: dict, db_conn):
        over = fresh_product["stock"] + 1
        r = create_order(api, fresh_user["token"], [{"product_id": fresh_product["id"], "qty": over}])
        assert_status(r, 409)
        assert_stock(db_conn, fresh_product["id"], fresh_product["stock"])

    @pytest.mark.parametrize("case", cases("order", "invalid_qty"), ids=lambda c: c["id"])
    def test_invalid_qty(self, api: ApiClient, fresh_user: dict, fresh_product: dict, case: dict):
        r = create_order(api, fresh_user["token"], [{"product_id": fresh_product["id"], "qty": case["qty"]}])
        assert_status(r, 400)

    @pytest.mark.parametrize("case", cases("order", "invalid_product"), ids=lambda c: c["id"])
    def test_invalid_product(self, api: ApiClient, fresh_user: dict, case: dict):
        r = create_order(api, fresh_user["token"], [{"product_id": case["product_id"], "qty": case["qty"]}])
        assert r.status_code in (400, 404), f"{case['desc']}: {r.status_code} {r.text}"

    def test_empty_items_rejected(self, api: ApiClient, fresh_user: dict):
        assert_status(create_order(api, fresh_user["token"], []), 400)


class TestCancelOrder:
    def test_cancel_releases_stock(self, api: ApiClient, fresh_user: dict, fresh_product: dict, db_conn):
        pid = fresh_product["id"]
        oid = create_order(api, fresh_user["token"], [{"product_id": pid, "qty": 3}]).json()["order_id"]
        stock_after_order = api.get(f"/api/products/{pid}").json()["stock"]

        assert_status(api.post(f"/api/orders/{oid}/cancel", token=fresh_user["token"]), 200)

        assert api.get(f"/api/products/{pid}").json()["stock"] == stock_after_order + 3
        assert_order_status(db_conn, oid, "cancelled")

    def test_cancel_paid_order_rejected(self, api: ApiClient, fresh_user: dict, fresh_product: dict):
        r = create_order(api, fresh_user["token"], [{"product_id": fresh_product["id"], "qty": 1}])
        oid, amount = r.json()["order_id"], r.json()["total_cents"]
        api.post(
            "/api/payments/callback",
            json={"order_id": oid, "amount_cents": amount, "idempotency_key": f"k{oid}"},
        )
        assert_status(api.post(f"/api/orders/{oid}/cancel", token=fresh_user["token"]), 409)

    def test_cancel_twice(self, api: ApiClient, fresh_user: dict, fresh_product: dict):
        oid = create_order(api, fresh_user["token"], [{"product_id": fresh_product["id"], "qty": 1}]).json()["order_id"]
        assert_status(api.post(f"/api/orders/{oid}/cancel", token=fresh_user["token"]), 200)
        assert_status(api.post(f"/api/orders/{oid}/cancel", token=fresh_user["token"]), 409)


class TestAuthz:
    def test_other_user_cannot_read_order(
        self, api: ApiClient, fresh_user: dict, another_user: dict, fresh_product: dict
    ):
        oid = create_order(
            api, fresh_user["token"], [{"product_id": fresh_product["id"], "qty": 1}]
        ).json()["order_id"]
        assert_status(api.get(f"/api/orders/{oid}", token=another_user["token"]), 403)

    def test_other_user_cannot_cancel(
        self, api: ApiClient, fresh_user: dict, another_user: dict, fresh_product: dict
    ):
        oid = create_order(
            api, fresh_user["token"], [{"product_id": fresh_product["id"], "qty": 1}]
        ).json()["order_id"]
        assert_status(api.post(f"/api/orders/{oid}/cancel", token=another_user["token"]), 403)

    def test_no_token_rejected(self, api: ApiClient):
        assert_status(api.get("/api/orders"), 401)
        assert_status(api.get("/api/orders", token="deadbeef"), 401)
