"""超时未支付自动取消。"""
from __future__ import annotations

from tests.api_client import ApiClient
from tests.assertions import assert_order_status, assert_status, assert_stock


def make_order(api: ApiClient, token: str, product_id: int, coupon_id: int | None = None) -> dict:
    r = api.post(
        "/api/orders",
        json={"items": [{"product_id": product_id, "qty": 1}], "coupon_id": coupon_id},
        token=token,
    )
    assert_status(r, 201)
    return r.json()


def expire_now(db_conn, order_id: int) -> None:
    db_conn.execute("UPDATE orders SET expire_at = datetime('now','-1 minute') WHERE id=?", (order_id,))
    db_conn.commit()


def expire_later(db_conn, order_id: int, minutes: int = 30) -> None:
    db_conn.execute(
        "UPDATE orders SET expire_at = datetime('now', ?) WHERE id=?", (f"+{minutes} minutes", order_id)
    )
    db_conn.commit()


class TestExpire:
    def test_expired_order_cancelled(self, api: ApiClient, fresh_user: dict, fresh_product: dict, db_conn):
        order = make_order(api, fresh_user["token"], fresh_product["id"])
        expire_now(db_conn, order["order_id"])

        r = api.post("/internal/expire-orders")
        assert_status(r, 200)
        assert r.json()["expired"] >= 1
        assert_order_status(db_conn, order["order_id"], "cancelled")

    def test_not_expired_untouched(self, api: ApiClient, fresh_user: dict, fresh_product: dict, db_conn):
        order = make_order(api, fresh_user["token"], fresh_product["id"])
        expire_later(db_conn, order["order_id"], 30)

        api.post("/internal/expire-orders")
        assert_order_status(db_conn, order["order_id"], "created")

    def test_expire_releases_stock(self, api: ApiClient, fresh_user: dict, fresh_product: dict, db_conn):
        order = make_order(api, fresh_user["token"], fresh_product["id"])
        expire_now(db_conn, order["order_id"])

        api.post("/internal/expire-orders")
        assert_stock(db_conn, fresh_product["id"], fresh_product["stock"])

    def test_expire_returns_coupon(self, api: ApiClient, fresh_user: dict, fresh_product: dict, db_conn):
        uc_id = api.post("/api/coupons/1/claim", token=fresh_user["token"]).json()["user_coupon_id"]
        order = make_order(api, fresh_user["token"], fresh_product["id"], coupon_id=uc_id)
        expire_now(db_conn, order["order_id"])

        api.post("/internal/expire-orders")
        mine = api.get("/api/coupons/mine", token=fresh_user["token"]).json()["items"]
        assert mine[0]["status"] == "unused"

    def test_paid_order_not_expired(self, api: ApiClient, fresh_user: dict, fresh_product: dict, db_conn):
        order = make_order(api, fresh_user["token"], fresh_product["id"])
        api.post(
            "/api/payments/callback",
            json={
                "order_id": order["order_id"],
                "amount_cents": order["payable_cents"],
                "idempotency_key": f"exp{order['order_id']}",
            },
        )
        expire_now(db_conn, order["order_id"])

        api.post("/internal/expire-orders")
        assert_order_status(db_conn, order["order_id"], "paid")

    def test_idempotent(self, api: ApiClient, fresh_user: dict, fresh_product: dict, db_conn):
        """连跑两次，第二次不该再处理同一单，库存也不能重复加。"""
        order = make_order(api, fresh_user["token"], fresh_product["id"])
        expire_now(db_conn, order["order_id"])

        first = api.post("/internal/expire-orders").json()["expired"]
        second = api.post("/internal/expire-orders").json()["expired"]

        assert first >= 1
        assert second == 0, f"第二次不该再取消任何订单，实际 {second}"
        assert_stock(db_conn, fresh_product["id"], fresh_product["stock"])

    def test_expired_minutes_set_on_create(self, api: ApiClient, fresh_user: dict, fresh_product: dict, db_conn):
        order = make_order(api, fresh_user["token"], fresh_product["id"])
        row = db_conn.execute("SELECT expire_at FROM orders WHERE id=?", (order["order_id"],)).fetchone()
        assert row["expire_at"] is not None, "下单时应写入过期时间"
