"""优惠券用例：领取、门槛、下单抵扣、分摊、退回。"""
from __future__ import annotations

import pytest

from tests.api_client import ApiClient, describe
from tests.assertions import assert_order_status, assert_status


def claim(api: ApiClient, token: str, coupon_id: int = 1) -> int:
    r = api.post(f"/api/coupons/{coupon_id}/claim", token=token)
    assert_status(r, 201)
    return r.json()["user_coupon_id"]


class TestClaim:
    def test_list_available(self, api: ApiClient):
        r = api.get("/api/coupons")
        assert_status(r, 200)
        items = r.json()["items"]
        assert len(items) >= 3
        assert all(i["remaining"] > 0 for i in items)

    def test_claim_success(self, api: ApiClient, fresh_user: dict):
        uc_id = claim(api, fresh_user["token"])
        mine = api.get("/api/coupons/mine", token=fresh_user["token"]).json()["items"]
        assert len(mine) == 1
        assert mine[0]["id"] == uc_id
        assert mine[0]["status"] == "unused"

    def test_claim_twice_rejected(self, api: ApiClient, fresh_user: dict):
        claim(api, fresh_user["token"])
        r = api.post("/api/coupons/1/claim", token=fresh_user["token"])
        assert_status(r, 409)

    def test_claim_not_exist(self, api: ApiClient, fresh_user: dict):
        r = api.post("/api/coupons/9999/claim", token=fresh_user["token"])
        assert_status(r, 404)

    def test_claim_requires_login(self, api: ApiClient):
        assert_status(api.post("/api/coupons/1/claim"), 401)

    def test_remaining_decreases(self, api: ApiClient, fresh_user: dict):
        before = {i["id"]: i["remaining"] for i in api.get("/api/coupons").json()["items"]}
        claim(api, fresh_user["token"], 1)
        after = {i["id"]: i["remaining"] for i in api.get("/api/coupons").json()["items"]}
        assert after[1] == before[1] - 1


class TestUseCoupon:
    def test_order_with_coupon(self, api: ApiClient, fresh_user: dict, fresh_product: dict):
        uc_id = claim(api, fresh_user["token"])
        p = fresh_product
        r = api.post(
            "/api/orders",
            json={"items": [{"product_id": p["id"], "qty": 1}], "coupon_id": uc_id},
            token=fresh_user["token"],
        )
        assert_status(r, 201)
        body = r.json()
        # 商品 10000 分，满 100 减 20
        assert body["total_cents"] == 10000
        assert body["discount_cents"] == 2000
        assert body["payable_cents"] == 8000

    @pytest.mark.parametrize("qty", [1, 2, 3])
    def test_payable_matches_pay_callback(
        self, api: ApiClient, fresh_user: dict, fresh_product: dict, qty: int
    ):
        uc_id = claim(api, fresh_user["token"])
        r = api.post(
            "/api/orders",
            json={"items": [{"product_id": fresh_product["id"], "qty": qty}], "coupon_id": uc_id},
            token=fresh_user["token"],
        )
        assert_status(r, 201)
        order = r.json()

        pay = api.post(
            "/api/payments/callback",
            json={
                "order_id": order["order_id"],
                "amount_cents": order["payable_cents"],
                "idempotency_key": f"pay{order['order_id']}",
            },
        )
        assert_status(pay, 200)

    def test_pay_full_price_rejected(self, api: ApiClient, fresh_user: dict, fresh_product: dict):
        uc_id = claim(api, fresh_user["token"])
        r = api.post(
            "/api/orders",
            json={"items": [{"product_id": fresh_product["id"], "qty": 1}], "coupon_id": uc_id},
            token=fresh_user["token"],
        )
        order = r.json()
        pay = api.post(
            "/api/payments/callback",
            json={
                "order_id": order["order_id"],
                "amount_cents": order["total_cents"],  # 没减券的钱
                "idempotency_key": f"bad{order['order_id']}",
            },
        )
        assert_status(pay, 400)

    def test_below_threshold_rejected(self, api: ApiClient, fresh_user: dict, make_product):
        uc_id = claim(api, fresh_user["token"])  # 门槛 10000
        p = make_product(price_cents=5000, stock=10)
        r = api.post(
            "/api/orders",
            json={"items": [{"product_id": p["id"], "qty": 1}], "coupon_id": uc_id},
            token=fresh_user["token"],
        )
        assert_status(r, 400)

    def test_coupon_cannot_be_used_twice(self, api: ApiClient, fresh_user: dict, fresh_product: dict):
        uc_id = claim(api, fresh_user["token"])
        first = api.post(
            "/api/orders",
            json={"items": [{"product_id": fresh_product["id"], "qty": 1}], "coupon_id": uc_id},
            token=fresh_user["token"],
        )
        assert_status(first, 201)

        second = api.post(
            "/api/orders",
            json={"items": [{"product_id": fresh_product["id"], "qty": 1}], "coupon_id": uc_id},
            token=fresh_user["token"],
        )
        assert_status(second, 409)

    def test_coupon_marked_used(self, api: ApiClient, fresh_user: dict, fresh_product: dict):
        uc_id = claim(api, fresh_user["token"])
        api.post(
            "/api/orders",
            json={"items": [{"product_id": fresh_product["id"], "qty": 1}], "coupon_id": uc_id},
            token=fresh_user["token"],
        )
        mine = api.get("/api/coupons/mine", token=fresh_user["token"]).json()["items"]
        assert mine[0]["status"] == "used"
        assert mine[0]["order_id"] is not None

    def test_coupon_from_another_user_rejected(
        self, api: ApiClient, fresh_user: dict, another_user: dict, fresh_product: dict
    ):
        uc_id = claim(api, fresh_user["token"])
        r = api.post(
            "/api/orders",
            json={"items": [{"product_id": fresh_product["id"], "qty": 1}], "coupon_id": uc_id},
            token=another_user["token"],
        )
        assert_status(r, 404)

    def test_discount_not_exceed_amount(self, api: ApiClient, fresh_user: dict, make_product):
        """券面额大于订单金额时，最多减到 0，不能变负数。"""
        uc_id = claim(api, fresh_user["token"])  # 减 2000
        p = make_product(price_cents=1000, stock=10)  # 10 元，低于门槛，先凑到门槛
        r = api.post(
            "/api/orders",
            json={"items": [{"product_id": p["id"], "qty": 10}], "coupon_id": uc_id},
            token=fresh_user["token"],
        )
        assert_status(r, 201)
        body = r.json()
        assert body["payable_cents"] >= 0
        assert body["discount_cents"] <= body["total_cents"]


class TestSplit:
    def test_split_sums_to_payable(self, api: ApiClient, fresh_user: dict, db_conn):
        """多商品用券，每项分摊额之和必须等于实付。"""
        uc_id = claim(api, fresh_user["token"], 2)  # 满 300 减 50
        r = api.post(
            "/api/orders",
            json={
                "items": [
                    {"product_id": 1, "qty": 1},  # 29900
                    {"product_id": 2, "qty": 1},  # 8900
                ],
                "coupon_id": uc_id,
            },
            token=fresh_user["token"],
        )
        assert_status(r, 201)
        body = r.json()
        assert body["discount_cents"] == 5000

        rows = db_conn.execute(
            "SELECT price_cents, qty, share_cents FROM order_items WHERE order_id=?",
            (body["order_id"],),
        ).fetchall()
        total_share = sum(r["share_cents"] for r in rows)
        assert total_share == body["payable_cents"], f"分摊合计 {total_share} 应等于实付 {body['payable_cents']}"

    def test_split_proportional(self, api: ApiClient, fresh_user: dict, db_conn):
        """贵的商品分摊得多。"""
        uc_id = claim(api, fresh_user["token"], 2)
        r = api.post(
            "/api/orders",
            json={
                "items": [
                    {"product_id": 1, "qty": 1},  # 29900
                    {"product_id": 2, "qty": 1},  # 8900
                ],
                "coupon_id": uc_id,
            },
            token=fresh_user["token"],
        )
        oid = r.json()["order_id"]
        rows = {
            row["product_id"]: row["share_cents"]
            for row in db_conn.execute(
                "SELECT product_id, share_cents FROM order_items WHERE order_id=?", (oid,)
            ).fetchall()
        }
        assert rows[1] > rows[2], f"键盘分摊 {rows[1]} 应多于鼠标 {rows[2]}"
        assert rows[1] + rows[2] == r.json()["payable_cents"]


class TestReturnCoupon:
    def test_cancel_returns_coupon(self, api: ApiClient, fresh_user: dict, fresh_product: dict):
        uc_id = claim(api, fresh_user["token"])
        oid = api.post(
            "/api/orders",
            json={"items": [{"product_id": fresh_product["id"], "qty": 1}], "coupon_id": uc_id},
            token=fresh_user["token"],
        ).json()["order_id"]

        assert_status(api.post(f"/api/orders/{oid}/cancel", token=fresh_user["token"]), 200)

        mine = api.get("/api/coupons/mine", token=fresh_user["token"]).json()["items"]
        assert mine[0]["status"] == "unused", "取消订单应把券退回"
        assert mine[0]["order_id"] is None
