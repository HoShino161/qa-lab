"""购物车接口用例。"""
from __future__ import annotations

import pytest

from tests.api_client import ApiClient, describe


class TestCart:
    def test_add_item(self, api: ApiClient, fresh_user: dict, fresh_product: dict):
        pid = fresh_product["id"]
        r = api.post("/api/cart", json={"product_id": pid, "qty": 2}, token=fresh_user["token"])
        assert r.status_code == 201, describe(r)

        items = api.get("/api/cart", token=fresh_user["token"]).json()["items"]
        assert len(items) == 1
        assert items[0]["product_id"] == pid
        assert items[0]["qty"] == 2

    def test_add_same_product_accumulates(self, api: ApiClient, fresh_user: dict, fresh_product: dict):
        pid = fresh_product["id"]
        api.post("/api/cart", json={"product_id": pid, "qty": 2}, token=fresh_user["token"])
        api.post("/api/cart", json={"product_id": pid, "qty": 3}, token=fresh_user["token"])

        items = api.get("/api/cart", token=fresh_user["token"]).json()["items"]
        assert len(items) == 1, "同一商品应合并成一条"
        assert items[0]["qty"] == 5

    def test_add_multiple_products(self, api: ApiClient, fresh_user: dict, fresh_product: dict):
        pid = fresh_product["id"]
        api.post("/api/cart", json={"product_id": pid, "qty": 1}, token=fresh_user["token"])
        api.post("/api/cart", json={"product_id": 1, "qty": 2}, token=fresh_user["token"])

        items = api.get("/api/cart", token=fresh_user["token"]).json()["items"]
        assert len(items) == 2

    @pytest.mark.parametrize("qty", [0, -1, -10])
    def test_invalid_qty_rejected(self, api: ApiClient, fresh_user: dict, fresh_product: dict, qty: int):
        r = api.post(
            "/api/cart", json={"product_id": fresh_product["id"], "qty": qty}, token=fresh_user["token"]
        )
        assert r.status_code == 400, describe(r)

    def test_cart_requires_login(self, api: ApiClient, fresh_product: dict):
        assert api.post("/api/cart", json={"product_id": fresh_product["id"], "qty": 1}).status_code == 401
        assert api.get("/api/cart").status_code == 401

    def test_cart_empty_for_new_user(self, api: ApiClient, fresh_user: dict):
        items = api.get("/api/cart", token=fresh_user["token"]).json()["items"]
        assert items == []

    def test_cart_isolated_between_users(self, api: ApiClient, fresh_user: dict, another_user: dict, fresh_product: dict):
        api.post("/api/cart", json={"product_id": fresh_product["id"], "qty": 2}, token=fresh_user["token"])

        items = api.get("/api/cart", token=another_user["token"]).json()["items"]
        assert items == [], "另一个用户不该看到别人的购物车"

    def test_order_clears_cart(self, api: ApiClient, fresh_user: dict, fresh_product: dict):
        pid = fresh_product["id"]
        api.post("/api/cart", json={"product_id": pid, "qty": 2}, token=fresh_user["token"])
        r = api.post("/api/orders", json={"items": [{"product_id": pid, "qty": 2}]}, token=fresh_user["token"])
        assert r.status_code == 201, describe(r)

        items = api.get("/api/cart", token=fresh_user["token"]).json()["items"]
        assert items == [], "下单后购物车应清空"

    def test_add_nonexistent_product(self, api: ApiClient, fresh_user: dict):
        r = api.post("/api/cart", json={"product_id": 999999, "qty": 1}, token=fresh_user["token"])
        assert r.status_code == 404, describe(r)
