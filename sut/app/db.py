"""建表与种子数据。"""
import os
import sqlite3
from pathlib import Path

DB_PATH = os.getenv("SUT_DB", str(Path(__file__).resolve().parent.parent / "shop.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password      TEXT NOT NULL,
    token         TEXT,
    role          TEXT NOT NULL DEFAULT 'user'
);

CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    stock       INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'on_sale'
);

CREATE TABLE IF NOT EXISTS cart_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    qty        INTEGER NOT NULL,
    UNIQUE(user_id, product_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'created',
    total_cents   INTEGER NOT NULL,
    discount_cents INTEGER NOT NULL DEFAULT 0,
    user_coupon_id INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    expire_at     TEXT
);

CREATE TABLE IF NOT EXISTS order_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL,
    product_id  INTEGER NOT NULL,
    qty         INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    share_cents INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL,
    amount_cents    INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'success'
);

CREATE TABLE IF NOT EXISTS coupons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    threshold_cents INTEGER NOT NULL,
    discount_cents  INTEGER NOT NULL,
    total           INTEGER NOT NULL,
    remaining       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_coupons (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    coupon_id  INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'unused',
    order_id   INTEGER,
    used_at    TEXT
);

CREATE TABLE IF NOT EXISTS refunds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'success',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_user_coupons_user ON user_coupons(user_id);
CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(order_id);
"""

SEED_PRODUCTS = [
    ("机械键盘", 29900, 100),
    ("无线鼠标", 8900, 50),
    ("显示器支架", 15900, 1),
]

SEED_COUPONS = [
    ("满 100 减 20", 10000, 2000, 100, 100),
    ("满 300 减 50", 30000, 5000, 50, 50),
    ("满 50 减 5", 5000, 500, 200, 200),
]

# 未支付订单的存活时间，超时由定时任务取消
ORDER_EXPIRE_MINUTES = 30


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补字段，避免改表结构就得删库。"""
    order_cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
    for name, ddl in [
        ("discount_cents", "ALTER TABLE orders ADD COLUMN discount_cents INTEGER NOT NULL DEFAULT 0"),
        ("user_coupon_id", "ALTER TABLE orders ADD COLUMN user_coupon_id INTEGER"),
        ("expire_at", "ALTER TABLE orders ADD COLUMN expire_at TEXT"),
    ]:
        if name not in order_cols:
            conn.execute(ddl)

    item_cols = {r["name"] for r in conn.execute("PRAGMA table_info(order_items)")}
    if "share_cents" not in item_cols:
        conn.execute("ALTER TABLE order_items ADD COLUMN share_cents INTEGER NOT NULL DEFAULT 0")


def init_db(reset: bool = False) -> None:
    if reset and Path(DB_PATH).exists():
        Path(DB_PATH).unlink()
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)

    if conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"] == 0:
        conn.executemany(
            "INSERT INTO products(name, price_cents, stock) VALUES (?,?,?)", SEED_PRODUCTS
        )
    if conn.execute("SELECT COUNT(*) AS c FROM coupons").fetchone()["c"] == 0:
        conn.executemany(
            "INSERT INTO coupons(name, threshold_cents, discount_cents, total, remaining) "
            "VALUES (?,?,?,?,?)",
            SEED_COUPONS,
        )
    conn.close()
