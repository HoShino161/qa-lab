"""支付回调用例。"""
from __future__ import annotations

import pytest

from tests.api_client import ApiClient, describe


def make_order(api: ApiClient, token: str, product_id: int = 1, qty: int = 1) -> tuple[int, int]:
    r = api.post("/api/orders", json={"items": [{"product_id": product_id, "qty": qty}]}, token=token)
    assert r.status_code == 201, describe(r)
    body = r.json()
    return body["order_id"], body["total_cents"]


class TestPaymentCallback:
    def test_pay_success(self, api: ApiClient, fresh_user: dict, db_conn):
        oid, amount = make_order(api, fresh_user["token"])

        r = api.post(
            "/api/payments/callback",
            json={"order_id": oid, "amount_cents": amount, "idempotency_key": f"pay-{oid}-1"},
        )
        assert r.status_code == 200, describe(r)
        assert r.json()["duplicated"] is False

        status = db_conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()["status"]
        assert status == "paid"

    def test_duplicate_callback_idempotent(self, api: ApiClient, fresh_user: dict, db_conn):
        oid, amount = make_order(api, fresh_user["token"])
        payload = {"order_id": oid, "amount_cents": amount, "idempotency_key": f"pay-{oid}-dup"}

        first = api.post("/api/payments/callback", json=payload)
        second = api.post("/api/payments/callback", json=payload)

        assert first.status_code == 200, describe(first)
        assert first.json()["duplicated"] is False

        # 幂等的正确表现是「照常返回 200 且明确标记重复」，
        # 不是把第二次回调当错误抛出去 —— 网关会一直重试，越重试越乱。
        assert second.status_code == 200, f"重复回调应幂等返回 200: {describe(second)}"
        assert second.json()["duplicated"] is True, f"重复回调未标记 duplicated: {describe(second)}"

        count = db_conn.execute(
            "SELECT COUNT(*) AS c FROM payments WHERE order_id=?", (oid,)
        ).fetchone()["c"]
        assert count == 1, f"同一幂等键只应产生一条支付记录，实际 {count} 条"

        status = db_conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()["status"]
        assert status == "paid", "重复回调不应改变订单状态"

    def test_amount_mismatch_rejected(self, api: ApiClient, fresh_user: dict):
        oid, amount = make_order(api, fresh_user["token"])

        r = api.post(
            "/api/payments/callback",
            json={"order_id": oid, "amount_cents": amount - 1, "idempotency_key": f"pay-{oid}-bad"},
        )
        assert r.status_code == 400, f"金额不符应拒绝: {describe(r)}"

    def test_pay_cancelled_order(self, api: ApiClient, fresh_user: dict, db_conn):
        oid, amount = make_order(api, fresh_user["token"])
        api.post(f"/api/orders/{oid}/cancel", token=fresh_user["token"])

        r = api.post(
            "/api/payments/callback",
            json={"order_id": oid, "amount_cents": amount, "idempotency_key": f"pay-{oid}-c"},
        )
        # 现状：服务端没用状态机拦，先记录实际行为，再由断言驱动修复
        status = db_conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()["status"]
        assert status == "cancelled", f"已取消订单不应被支付改写状态，实际 {status}（接口 {r.status_code}）"

    def test_pay_not_exist_order(self, api: ApiClient):
        r = api.post(
            "/api/payments/callback",
            json={"order_id": 999999, "amount_cents": 100, "idempotency_key": "nope"},
        )
        assert r.status_code == 404, describe(r)
