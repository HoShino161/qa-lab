"""并发用例。requests.Session 不是线程安全的，每个线程各建一个。"""
from __future__ import annotations

import concurrent.futures as futures
import uuid

import requests

from tests.api_client import ApiClient


def _new_user_token(api: ApiClient) -> str:
    name = f"c{uuid.uuid4().hex[:10]}"
    api.post("/api/auth/register", json={"username": name, "password": "pass123456"})
    return api.post("/api/auth/login", json={"username": name, "password": "pass123456"}).json()["token"]


def _place_order(base_url: str, token: str, product_id: int, qty: int = 1) -> int:
    s = requests.Session()
    s.trust_env = False
    try:
        r = s.post(
            f"{base_url}/api/orders",
            json={"items": [{"product_id": product_id, "qty": qty}]},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        return r.status_code
    except requests.RequestException:
        return -1
    finally:
        s.close()


class TestConcurrency:
    def test_no_oversell_under_concurrency(self, api: ApiClient, base_url: str, scarce_product: dict, db_conn):
        pid = scarce_product["id"]
        workers = scarce_product["stock"] + 9  # 1 件库存，10 个人抢

        tokens = [_new_user_token(api) for _ in range(workers)]

        with futures.ThreadPoolExecutor(max_workers=workers) as pool:
            codes = list(pool.map(lambda t: _place_order(base_url, t, pid), tokens))

        success = codes.count(201)
        stock_after = db_conn.execute("SELECT stock FROM products WHERE id=?", (pid,)).fetchone()["stock"]

        detail = f"库存 {scarce_product['stock']}，成功 {success}，状态码分布 {sorted(codes)}"
        assert success <= scarce_product["stock"], f"发生超卖：{detail}"
        assert stock_after >= 0, f"库存被扣成负数：{stock_after}，{detail}"

    def test_same_user_concurrent_orders(self, api: ApiClient, base_url: str, fresh_user: dict, fresh_product: dict, db_conn):
        """同一用户并发下单：订单落库数要等于成功响应数，不能有半截订单。"""
        pid = fresh_product["id"]
        before = db_conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]

        with futures.ThreadPoolExecutor(max_workers=5) as pool:
            codes = list(
                pool.map(lambda _: _place_order(base_url, fresh_user["token"], pid), range(5))
            )

        after = db_conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
        created = after - before
        assert created == codes.count(201), f"订单落库 {created} 条，成功响应 {codes.count(201)} 条，对不上"
