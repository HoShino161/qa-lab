"""统一断言。"""
from __future__ import annotations

import sqlite3

import requests

from tests.api_client import describe


def assert_status(resp: requests.Response, expected: int) -> None:
    assert resp.status_code == expected, describe(resp)


def assert_field(body: dict, **expected) -> None:
    for key, value in expected.items():
        actual = body.get(key)
        assert actual == value, f"字段 {key} 期望 {value}，实际 {actual}；响应 {body}"


def assert_order_status(db: sqlite3.Connection, order_id: int, expected: str) -> None:
    row = db.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
    assert row is not None, f"订单 {order_id} 不存在"
    assert row["status"] == expected, f"订单 {order_id} 应为 {expected}，实际 {row['status']}"


def assert_stock(db: sqlite3.Connection, product_id: int, expected: int) -> None:
    row = db.execute("SELECT stock FROM products WHERE id=?", (product_id,)).fetchone()
    assert row is not None, f"商品 {product_id} 不存在"
    assert row["stock"] == expected, f"商品 {product_id} 库存应为 {expected}，实际 {row['stock']}"
