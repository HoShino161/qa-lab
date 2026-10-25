"""用例夹具。"""
from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path

import pytest

from tests.api_client import ApiClient

BASE_URL = os.getenv("SUT_BASE_URL", "http://127.0.0.1:8000")
DB_PATH = os.getenv("SUT_DB", str(Path(__file__).resolve().parent.parent / "shop.db"))


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def api(base_url: str):
    client = ApiClient(base_url)
    yield client
    client.close()


@pytest.fixture
def fresh_user(api: ApiClient) -> dict:
    name = f"u{uuid.uuid4().hex[:10]}"
    pwd = "pass123456"

    r = api.post("/api/auth/register", json={"username": name, "password": pwd})
    assert r.status_code == 201, f"注册失败: {r.status_code} {r.text}"

    r = api.post("/api/auth/login", json={"username": name, "password": pwd})
    assert r.status_code == 200, f"登录失败: {r.status_code} {r.text}"

    return {"username": name, "password": pwd, "token": r.json()["token"], "id": r.json()["user_id"]}


@pytest.fixture
def another_user(api: ApiClient) -> dict:
    name = f"v{uuid.uuid4().hex[:10]}"
    pwd = "pass123456"
    api.post("/api/auth/register", json={"username": name, "password": pwd})
    token = api.post("/api/auth/login", json={"username": name, "password": pwd}).json()["token"]
    return {"username": name, "token": token}


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def fresh_product(db_conn):
    """独立商品，避开共享库存。"""
    tag = uuid.uuid4().hex[:6]
    cur = db_conn.execute(
        "INSERT INTO products(name, price_cents, stock) VALUES (?,?,?)",
        (f"测试商品-{tag}", 10000, 5),
    )
    db_conn.commit()
    pid = cur.lastrowid

    yield {"id": pid, "price_cents": 10000, "stock": 5}

    db_conn.execute("DELETE FROM order_items WHERE product_id=?", (pid,))
    db_conn.execute("DELETE FROM products WHERE id=?", (pid,))
    db_conn.commit()


@pytest.fixture
def scarce_product(db_conn):
    """库存 1 件。"""
    tag = uuid.uuid4().hex[:6]
    cur = db_conn.execute(
        "INSERT INTO products(name, price_cents, stock) VALUES (?,?,?)",
        (f"稀缺商品-{tag}", 10000, 1),
    )
    db_conn.commit()
    pid = cur.lastrowid

    yield {"id": pid, "price_cents": 10000, "stock": 1}

    db_conn.execute("DELETE FROM order_items WHERE product_id=?", (pid,))
    db_conn.execute("DELETE FROM products WHERE id=?", (pid,))
    db_conn.commit()


@pytest.fixture
def odd_price_product(db_conn):
    """5.01 元。5.01 × 5 用浮点算是 2504 分，整数是 2505。"""
    tag = uuid.uuid4().hex[:6]
    cur = db_conn.execute(
        "INSERT INTO products(name, price_cents, stock) VALUES (?,?,?)",
        (f"零头商品-{tag}", 501, 10),
    )
    db_conn.commit()
    pid = cur.lastrowid

    yield {"id": pid, "price_cents": 501, "stock": 10}

    db_conn.execute("DELETE FROM order_items WHERE product_id=?", (pid,))
    db_conn.execute("DELETE FROM products WHERE id=?", (pid,))
    db_conn.commit()


@pytest.fixture
def make_product(db_conn):
    """按需造商品，价格和库存都能指定。用例结束后统一清理。"""
    created: list[int] = []

    def _make(price_cents: int = 10000, stock: int = 5, prefix: str = "测试商品") -> dict:
        tag = uuid.uuid4().hex[:6]
        cur = db_conn.execute(
            "INSERT INTO products(name, price_cents, stock) VALUES (?,?,?)",
            (f"{prefix}-{tag}", price_cents, stock),
        )
        db_conn.commit()
        pid = cur.lastrowid
        created.append(pid)
        return {"id": pid, "price_cents": price_cents, "stock": stock}

    yield _make

    for pid in created:
        db_conn.execute("DELETE FROM order_items WHERE product_id=?", (pid,))
        db_conn.execute("DELETE FROM products WHERE id=?", (pid,))
    db_conn.commit()


@pytest.fixture(scope="session", autouse=True)
def _sut_reachable(api: ApiClient):
    try:
        r = api.get("/health")
    except Exception as exc:  # noqa: BLE001
        pytest.exit(f"被测服务不可达 {BASE_URL}: {exc}", returncode=2)
    assert r.status_code == 200, f"健康检查失败: {r.text}"
