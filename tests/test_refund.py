"""退款用例：全额、部分、超额、状态机、库存与券的回退。"""
from __future__ import annotations

import pytest

from tests.api_client import ApiClient
from tests.assertions import assert_order_status, assert_status, assert_stock


def paid_order(api: ApiClient, token: str, product_id: int, qty: int = 1, coupon_id: int | None = None) -> dict:
    r = api.post(
        "/api/orders",
        json={"items": [{"product_id": product_id, "qty": qty}], "coupon_id": coupon_id},
        token=token,
    )
    assert_status(r, 201)
    order = r.json()
    pay = api.post(
        "/api/payments/callback",
        json={
            "order_id": order["order_id"],
            "amount_cents": order["payable_cents"],
            "idempotency_key": f"refund-{order['order_id']}",
        },
    )
    assert_status(pay, 200)
    return order


class TestFullRefund:
    def test_full_refund(self, api: ApiClient, fresh_user: dict, fresh_product: dict, db_conn):
        order = paid_order(api, fresh_user["token"], fresh_product["id"], qty=2)

        r = api.post(
            f"/api/orders/{order['order_id']}/refund",
            json={"reason": "不想要了"},
            token=fresh_user["token"],
        )
        assert_status(r, 201)
        assert r.json()["full"] is True
        assert r.json()["refunded_cents"] == order["payable_cents"]
        assert_order_status(db_conn, order["order_id"], "refunded")
        assert_stock(db_conn, fresh_product["id"], fresh_product["stock"])

    def test_refund_returns_coupon(self, api: ApiClient, fresh_user: dict, fresh_product: dict):
        uc_id = api.post("/api/coupons/1/claim", token=fresh_user["token"]).json()["user_coupon_id"]
        order = paid_order(api, fresh_user["token"], fresh_product["id"], coupon_id=uc_id)

        api.post(f"/api/orders/{order['order_id']}/refund", json={}, token=fresh_user["token"])

        mine = api.get("/api/coupons/mine", token=fresh_user["token"]).json()["items"]
        assert mine[0]["status"] == "unused", "全额退款应退券"

    def test_refund_amount_is_payable_not_gross(
        self, api: ApiClient, fresh_user: dict, fresh_product: dict
    ):
        """用券订单退款，退的是实付金额，不是原价。"""
        uc_id = api.post("/api/coupons/1/claim", token=fresh_user["token"]).json()["user_coupon_id"]
        order = paid_order(api, fresh_user["token"], fresh_product["id"], coupon_id=uc_id)

        r = api.post(f"/api/orders/{order['order_id']}/refund", json={}, token=fresh_user["token"])
        assert_status(r, 201)
        assert r.json()["refunded_cents"] == order["payable_cents"]
        assert r.json()["refunded_cents"] < order["total_cents"]

    def test_refund_twice_rejected(
        self, api: ApiClient, fresh_user: dict, fresh_product: dict, db_conn
    ):
        order = paid_order(api, fresh_user["token"], fresh_product["id"])
        api.post(f"/api/orders/{order['order_id']}/refund", json={}, token=fresh_user["token"])

        r = api.post(f"/api/orders/{order['order_id']}/refund", json={}, token=fresh_user["token"])
        assert_status(r, 409)


class TestPartialRefund:
    def test_partial_refund(self, api: ApiClient, fresh_user: dict, fresh_product: dict, db_conn):
        order = paid_order(api, fresh_user["token"], fresh_product["id"], qty=2)
        half = order["payable_cents"] // 2

        r = api.post(
            f"/api/orders/{order['order_id']}/refund",
            json={"amount_cents": half},
            token=fresh_user["token"],
        )
        assert_status(r, 201)
        assert r.json()["full"] is False
        assert_order_status(db_conn, order["order_id"], "partially_refunded")
        # 部分退款不动库存
        assert_stock(db_conn, fresh_product["id"], fresh_product["stock"] - 2)

    def test_partial_then_full(self, api: ApiClient, fresh_user: dict, fresh_product: dict, db_conn):
        order = paid_order(api, fresh_user["token"], fresh_product["id"], qty=2)
        total = order["payable_cents"]
        first = total // 3

        api.post(
            f"/api/orders/{order['order_id']}/refund",
            json={"amount_cents": first},
            token=fresh_user["token"],
        )
        r = api.post(
            f"/api/orders/{order['order_id']}/refund",
            json={"amount_cents": total - first},
            token=fresh_user["token"],
        )
        assert_status(r, 201)
        assert r.json()["full"] is True
        assert r.json()["total_refunded"] == total
        assert_order_status(db_conn, order["order_id"], "refunded")
        assert_stock(db_conn, fresh_product["id"], fresh_product["stock"])

    @pytest.mark.parametrize("delta", [1, 100, 10000])
    def test_exceed_remain_rejected(
        self, api: ApiClient, fresh_user: dict, fresh_product: dict, delta: int
    ):
        order = paid_order(api, fresh_user["token"], fresh_product["id"])
        r = api.post(
            f"/api/orders/{order['order_id']}/refund",
            json={"amount_cents": order["payable_cents"] + delta},
            token=fresh_user["token"],
        )
        assert_status(r, 400)

    @pytest.mark.parametrize("amount", [0, -1, -1000])
    def test_invalid_amount_rejected(
        self, api: ApiClient, fresh_user: dict, fresh_product: dict, amount: int
    ):
        order = paid_order(api, fresh_user["token"], fresh_product["id"])
        r = api.post(
            f"/api/orders/{order['order_id']}/refund",
            json={"amount_cents": amount},
            token=fresh_user["token"],
        )
        assert_status(r, 400)

    def test_partial_sum_equals_payable(
        self, api: ApiClient, fresh_user: dict, fresh_product: dict
    ):
        """拆成三次退，累计必须正好等于实付。"""
        order = paid_order(api, fresh_user["token"], fresh_product["id"], qty=3)
        total = order["payable_cents"]
        parts = [total // 3, total // 3, total - 2 * (total // 3)]

        for part in parts:
            r = api.post(
                f"/api/orders/{order['order_id']}/refund",
                json={"amount_cents": part},
                token=fresh_user["token"],
            )
            assert_status(r, 201)

        last = api.get(f"/api/orders/{order['order_id']}", token=fresh_user["token"]).json()
        assert last["status"] == "refunded"


class TestRefundGuards:
    def test_unpaid_order_cannot_refund(self, api: ApiClient, fresh_user: dict, fresh_product: dict):
        oid = api.post(
            "/api/orders",
            json={"items": [{"product_id": fresh_product["id"], "qty": 1}]},
            token=fresh_user["token"],
        ).json()["order_id"]

        r = api.post(f"/api/orders/{oid}/refund", json={}, token=fresh_user["token"])
        assert_status(r, 409)

    def test_other_user_cannot_refund(
        self, api: ApiClient, fresh_user: dict, another_user: dict, fresh_product: dict
    ):
        order = paid_order(api, fresh_user["token"], fresh_product["id"])
        r = api.post(
            f"/api/orders/{order['order_id']}/refund", json={}, token=another_user["token"]
        )
        assert_status(r, 403)

    def test_refund_not_exist_order(self, api: ApiClient, fresh_user: dict):
        r = api.post("/api/orders/999999/refund", json={}, token=fresh_user["token"])
        assert_status(r, 404)

    def test_refund_requires_login(self, api: ApiClient):
        assert_status(api.post("/api/orders/1/refund", json={}), 401)

    def test_cancelled_order_cannot_refund(
        self, api: ApiClient, fresh_user: dict, fresh_product: dict
    ):
        oid = api.post(
            "/api/orders",
            json={"items": [{"product_id": fresh_product["id"], "qty": 1}]},
            token=fresh_user["token"],
        ).json()["order_id"]
        api.post(f"/api/orders/{oid}/cancel", token=fresh_user["token"])

        assert_status(api.post(f"/api/orders/{oid}/refund", json={}, token=fresh_user["token"]), 409)
