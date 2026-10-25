"""简化版电商订单服务。金额用分存整数，库存扣减在事务内完成。"""
import hashlib
import sqlite3
import threading
import time
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import faults
from .db import ORDER_EXPIRE_MINUTES, connect, init_db

app = FastAPI(title="Shop SUT", version="0.1.0")


# ---------- 指标采集 ----------
# 手写 Prometheus 文本格式，不引 prometheus_client：
# 压测时只需要看请求量、耗时、库存、订单状态，这几项用字典计一下够了。

_metric_lock = threading.Lock()
_req_count: dict[tuple[str, str, int], int] = {}
_req_duration: dict[str, list[float]] = {}  # path -> [累计秒, 次数]


@app.middleware("http")
async def instrument(request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - started

    path = request.url.path
    if path != "/metrics":  # 自己统计自己会把平均值拉低
        with _metric_lock:
            key = (request.method, path, response.status_code)
            _req_count[key] = _req_count.get(key, 0) + 1
            slot = _req_duration.setdefault(path, [0.0, 0])
            slot[0] += elapsed
            slot[1] += 1
    return response


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    lines: list[str] = []

    lines.append("# HELP shop_http_requests_total HTTP 请求总数")
    lines.append("# TYPE shop_http_requests_total counter")
    with _metric_lock:
        counts = dict(_req_count)
        durations = {k: list(v) for k, v in _req_duration.items()}
    for (method, path, status), n in sorted(counts.items()):
        lines.append(
            f'shop_http_requests_total{{method="{method}",path="{path}",status="{status}"}} {n}'
        )

    lines.append("# HELP shop_http_request_duration_seconds_sum 请求耗时累计（秒）")
    lines.append("# TYPE shop_http_request_duration_seconds_sum counter")
    lines.append("# HELP shop_http_request_duration_seconds_count 请求次数")
    lines.append("# TYPE shop_http_request_duration_seconds_count counter")
    for path, (total, n) in sorted(durations.items()):
        lines.append(f'shop_http_request_duration_seconds_sum{{path="{path}"}} {total:.6f}')
        lines.append(f'shop_http_request_duration_seconds_count{{path="{path}"}} {n}')

    conn = connect()
    lines.append("# HELP shop_stock 商品库存")
    lines.append("# TYPE shop_stock gauge")
    for row in conn.execute("SELECT name, stock FROM products ORDER BY id").fetchall():
        lines.append(f'shop_stock{{product="{row["name"]}"}} {row["stock"]}')

    lines.append("# HELP shop_orders_total 订单数（按状态）")
    lines.append("# TYPE shop_orders_total gauge")
    for row in conn.execute(
        "SELECT status, COUNT(*) AS c FROM orders GROUP BY status ORDER BY status"
    ).fetchall():
        lines.append(f'shop_orders_total{{status="{row["status"]}"}} {row["c"]}')
    conn.close()

    return "\n".join(lines) + "\n"


# ---------- 基础 ----------

def hash_pwd(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def current_user(authorization: Optional[str] = Header(None)) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, detail="missing token")
    token = authorization.split(" ", 1)[1]
    conn = connect()
    row = conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(401, detail="invalid token")
    return row


def money(product_price_cents: int, qty: int) -> int:
    """金额计算。

    正确实现：整数分直接相乘。
    注入 float_money 后：转成元、用 float 累加、再乘回 100 —— 制造精度误差。
    """
    if faults.on("float_money"):
        total = 0.0
        for _ in range(qty):
            total += product_price_cents / 100.0
        return int(total * 100)
    return product_price_cents * qty


# ---------- 请求体 ----------

class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=64)


class LoginIn(BaseModel):
    username: str
    password: str


class CartIn(BaseModel):
    product_id: int
    qty: int


class OrderIn(BaseModel):
    items: list[CartIn]
    coupon_id: int | None = None


class PayIn(BaseModel):
    order_id: int
    amount_cents: int
    idempotency_key: str


class RefundIn(BaseModel):
    amount_cents: int | None = None
    reason: str = ""


class CouponIn(BaseModel):
    coupon_id: int


def split_amount(gross: int, payable: int, lines: list[tuple[int, int, int]]) -> list[int]:
    """按各商品原价占比分摊实付金额，余数给最后一项。

    部分退款要按 items 的实付额退，不能按原价，否则退多了。
    """
    if gross <= 0 or not lines:
        return [0] * len(lines)
    if faults.on("no_split"):
        return [price * qty for _pid, qty, price in lines]
    shares: list[int] = []
    allocated = 0
    for i, (_pid, qty, price) in enumerate(lines):
        if i == len(lines) - 1:
            shares.append(payable - allocated)
        else:
            part = price * qty * payable // gross
            shares.append(part)
            allocated += part
    return shares


def release_stock(conn: sqlite3.Connection, order_id: int) -> None:
    for item in conn.execute(
        "SELECT product_id, qty FROM order_items WHERE order_id=?", (order_id,)
    ).fetchall():
        conn.execute(
            "UPDATE products SET stock = stock + ? WHERE id=?",
            (item["qty"], item["product_id"]),
        )


def return_coupon(conn: sqlite3.Connection, order: sqlite3.Row) -> None:
    if order["user_coupon_id"]:
        conn.execute(
            "UPDATE user_coupons SET status='unused', order_id=NULL, used_at=NULL WHERE id=?",
            (order["user_coupon_id"],),
        )


# ---------- 账号 ----------

@app.get("/health")
def health():
    return {"status": "ok", "faults": faults.enabled_faults()}


@app.post("/api/auth/register", status_code=201)
def register(body: RegisterIn):
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO users(username, password, token) VALUES (?,?,?)",
            (body.username, hash_pwd(body.password), uuid.uuid4().hex),
        )
        uid = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(409, detail="username taken")
    row = conn.execute("SELECT id, username, token FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return {"id": row["id"], "username": row["username"], "token": row["token"]}


@app.post("/api/auth/login")
def login(body: LoginIn):
    conn = connect()
    row = conn.execute(
        "SELECT * FROM users WHERE username=? AND password=?",
        (body.username, hash_pwd(body.password)),
    ).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(401, detail="bad credentials")
    token = uuid.uuid4().hex
    conn.execute("UPDATE users SET token=? WHERE id=?", (token, row["id"]))
    conn.close()
    return {"token": token, "user_id": row["id"]}


@app.get("/api/users/me")
def me(user: sqlite3.Row = Depends(current_user)):
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


# ---------- 商品 ----------

@app.get("/api/products")
def list_products():
    conn = connect()
    rows = conn.execute(
        "SELECT id, name, price_cents, stock, status FROM products ORDER BY id"
    ).fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@app.get("/api/products/{pid}")
def get_product(pid: int):
    conn = connect()
    row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(404, detail="product not found")
    return dict(row)


# ---------- 购物车 ----------

@app.post("/api/cart", status_code=201)
def add_cart(body: CartIn, user: sqlite3.Row = Depends(current_user)):
    if not faults.on("no_qty_check") and body.qty <= 0:
        raise HTTPException(400, detail="qty must be positive")
    conn = connect()
    p = conn.execute("SELECT id FROM products WHERE id=?", (body.product_id,)).fetchone()
    if p is None:
        conn.close()
        raise HTTPException(404, detail="product not found")
    conn.execute(
        "INSERT INTO cart_items(user_id, product_id, qty) VALUES (?,?,?) "
        "ON CONFLICT(user_id, product_id) DO UPDATE SET qty = qty + excluded.qty",
        (user["id"], body.product_id, body.qty),
    )
    conn.close()
    return {"ok": True}


@app.get("/api/cart")
def get_cart(user: sqlite3.Row = Depends(current_user)):
    conn = connect()
    rows = conn.execute(
        "SELECT c.id, c.product_id, p.name, p.price_cents, c.qty "
        "FROM cart_items c JOIN products p ON p.id = c.product_id WHERE c.user_id=?",
        (user["id"],),
    ).fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows]}


# ---------- 下单 ----------

@app.post("/api/orders", status_code=201)
def create_order(body: OrderIn, user: sqlite3.Row = Depends(current_user)):
    if not body.items:
        raise HTTPException(400, detail="empty order")

    conn = connect()
    try:
        # 整个下单过程必须在一个事务里：先查库存再扣，中间被人插队就会超卖
        conn.execute("BEGIN IMMEDIATE")

        gross = 0
        lines = []
        for item in body.items:
            if not faults.on("no_qty_check") and item.qty <= 0:
                raise HTTPException(400, detail="qty must be positive")

            p = conn.execute("SELECT * FROM products WHERE id=?", (item.product_id,)).fetchone()
            if p is None:
                raise HTTPException(404, detail=f"product {item.product_id} not found")

            # 库存校验：oversell 开关关掉时才会检查
            if not faults.on("oversell") and p["stock"] < item.qty:
                if faults.on("wrong_status_code"):
                    conn.execute("COMMIT")
                    return {"code": 0, "msg": "库存不足"}
                raise HTTPException(409, detail=f"stock not enough for {p['name']}")

            gross += money(p["price_cents"], item.qty)
            lines.append((item.product_id, item.qty, p["price_cents"]))

        discount = 0
        user_coupon = None
        if body.coupon_id:
            user_coupon = conn.execute(
                "SELECT uc.id, uc.status, c.threshold_cents, c.discount_cents "
                "FROM user_coupons uc JOIN coupons c ON c.id = uc.coupon_id "
                "WHERE uc.id=? AND uc.user_id=?",
                (body.coupon_id, user["id"]),
            ).fetchone()
            if user_coupon is None:
                raise HTTPException(404, detail="coupon not found")
            if user_coupon["status"] != "unused":
                raise HTTPException(409, detail=f"coupon already {user_coupon['status']}")
            if gross < user_coupon["threshold_cents"]:
                raise HTTPException(400, detail="order amount below coupon threshold")
            discount = min(user_coupon["discount_cents"], gross)

        payable = gross - discount
        shares = split_amount(gross, payable, lines)

        cur = conn.execute(
            "INSERT INTO orders(user_id, status, total_cents, discount_cents, user_coupon_id, expire_at) "
            "VALUES (?,?,?,?,?, datetime('now', ?))",
            (
                user["id"],
                "created",
                gross,
                discount,
                body.coupon_id,
                f"+{ORDER_EXPIRE_MINUTES} minutes",
            ),
        )
        oid = cur.lastrowid
        conn.executemany(
            "INSERT INTO order_items(order_id, product_id, qty, price_cents, share_cents) "
            "VALUES (?,?,?,?,?)",
            [(oid, pid, qty, price, shares[i]) for i, (pid, qty, price) in enumerate(lines)],
        )

        for pid, qty, _ in lines:
            if faults.on("oversell"):
                conn.execute("UPDATE products SET stock = stock - ? WHERE id=?", (qty, pid))
            else:
                # 条件更新：库存够才扣，rowcount=0 说明被并发抢走了
                upd = conn.execute(
                    "UPDATE products SET stock = stock - ? WHERE id=? AND stock >= ?",
                    (qty, pid, qty),
                )
                if upd.rowcount == 0:
                    raise HTTPException(409, detail=f"stock not enough for product {pid}")

        if user_coupon and not faults.on("coupon_reuse"):
            conn.execute(
                "UPDATE user_coupons SET status='used', order_id=?, used_at=datetime('now') WHERE id=?",
                (oid, body.coupon_id),
            )
        conn.execute("DELETE FROM cart_items WHERE user_id=?", (user["id"],))
        conn.execute("COMMIT")
    except HTTPException:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return {
        "order_id": oid,
        "status": "created",
        "total_cents": gross,
        "discount_cents": discount,
        "payable_cents": payable,
    }


@app.get("/api/orders")
def list_orders(
    user: sqlite3.Row = Depends(current_user),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    conn = connect()
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE user_id=?", (user["id"],)
    ).fetchone()["c"]
    rows = conn.execute(
        "SELECT id, status, total_cents, created_at FROM orders WHERE user_id=? "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (user["id"], limit, offset),
    ).fetchall()
    conn.close()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/orders/{oid}")
def get_order(oid: int, user: sqlite3.Row = Depends(current_user)):
    conn = connect()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(404, detail="order not found")
    # 越权校验：no_authz 打开时跳过
    if not faults.on("no_authz") and row["user_id"] != user["id"]:
        conn.close()
        raise HTTPException(403, detail="not your order")
    items = conn.execute(
        "SELECT product_id, qty, price_cents FROM order_items WHERE order_id=?", (oid,)
    ).fetchall()
    conn.close()
    data = dict(row)
    data["items"] = [dict(i) for i in items]
    return data


@app.post("/api/orders/{oid}/cancel")
def cancel_order(oid: int, user: sqlite3.Row = Depends(current_user)):
    conn = connect()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(404, detail="order not found")
    if row["user_id"] != user["id"]:
        conn.close()
        raise HTTPException(403, detail="not your order")

    # 状态机：只有 created 允许取消
    if not faults.on("bad_state_machine") and row["status"] != "created":
        conn.close()
        raise HTTPException(409, detail=f"cannot cancel order in status {row['status']}")

    conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
    if not faults.on("no_release_stock"):
        release_stock(conn, oid)
        return_coupon(conn, row)
    conn.close()
    return {"order_id": oid, "status": "cancelled"}


# ---------- 支付回调 ----------

@app.post("/api/payments/callback")
def pay_callback(body: PayIn):
    conn = connect()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (body.order_id,)).fetchone()
    if order is None:
        conn.close()
        raise HTTPException(404, detail="order not found")
    payable = order["total_cents"] - order["discount_cents"]
    if payable != body.amount_cents:
        conn.close()
        raise HTTPException(400, detail="amount mismatch")

    # 幂等：靠 idempotency_key 唯一约束拦重复回调
    if not faults.on("no_idempotent"):
        exist = conn.execute(
            "SELECT id FROM payments WHERE idempotency_key=?", (body.idempotency_key,)
        ).fetchone()
        if exist:
            conn.close()
            return {"ok": True, "duplicated": True}

    try:
        conn.execute(
            "INSERT INTO payments(order_id, amount_cents, idempotency_key) VALUES (?,?,?)",
            (body.order_id, body.amount_cents, body.idempotency_key),
        )
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(409, detail="duplicated payment")

    conn.execute("UPDATE orders SET status='paid' WHERE id=? AND status='created'", (body.order_id,))
    conn.close()
    return {"ok": True, "duplicated": False}


# ---------- 优惠券 ----------

@app.get("/api/coupons")
def list_coupons():
    conn = connect()
    rows = conn.execute(
        "SELECT id, name, threshold_cents, discount_cents, remaining FROM coupons "
        "WHERE remaining > 0 ORDER BY id"
    ).fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/coupons/{cid}/claim", status_code=201)
def claim_coupon(cid: int, user: sqlite3.Row = Depends(current_user)):
    conn = connect()
    uc_id = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        c = conn.execute("SELECT * FROM coupons WHERE id=?", (cid,)).fetchone()
        if c is None:
            raise HTTPException(404, detail="coupon not found")
        if c["remaining"] <= 0:
            raise HTTPException(409, detail="coupon sold out")
        exist = conn.execute(
            "SELECT id FROM user_coupons WHERE user_id=? AND coupon_id=?", (user["id"], cid)
        ).fetchone()
        if exist:
            raise HTTPException(409, detail="already claimed")

        cur = conn.execute(
            "INSERT INTO user_coupons(user_id, coupon_id) VALUES (?,?)", (user["id"], cid)
        )
        uc_id = cur.lastrowid
        conn.execute("UPDATE coupons SET remaining = remaining - 1 WHERE id=?", (cid,))
        conn.execute("COMMIT")
    except HTTPException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return {"user_coupon_id": uc_id, "coupon_id": cid}


@app.get("/api/coupons/mine")
def my_coupons(user: sqlite3.Row = Depends(current_user)):
    conn = connect()
    rows = conn.execute(
        "SELECT uc.id, uc.coupon_id, c.name, c.threshold_cents, c.discount_cents, "
        "uc.status, uc.order_id FROM user_coupons uc "
        "JOIN coupons c ON c.id = uc.coupon_id WHERE uc.user_id=? ORDER BY uc.id DESC",
        (user["id"],),
    ).fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows]}


# ---------- 退款 ----------

@app.post("/api/orders/{oid}/refund", status_code=201)
def refund_order(oid: int, body: RefundIn, user: sqlite3.Row = Depends(current_user)):
    conn = connect()
    result = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if order is None:
            raise HTTPException(404, detail="order not found")
        if order["user_id"] != user["id"]:
            raise HTTPException(403, detail="not your order")
        if order["status"] not in ("paid", "partially_refunded"):
            raise HTTPException(409, detail=f"cannot refund order in status {order['status']}")

        payable = order["total_cents"] - order["discount_cents"]
        refunded = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM refunds "
            "WHERE order_id=? AND status='success'",
            (oid,),
        ).fetchone()["s"]
        remain = payable - refunded
        amount = body.amount_cents if body.amount_cents is not None else remain
        if not faults.on("refund_over") and (amount <= 0 or amount > remain):
            raise HTTPException(400, detail=f"refund amount must be 1..{remain}")

        conn.execute(
            "INSERT INTO refunds(order_id, amount_cents, reason) VALUES (?,?,?)",
            (oid, amount, body.reason),
        )
        full = refunded + amount == payable
        if full:
            conn.execute("UPDATE orders SET status='refunded' WHERE id=?", (oid,))
            release_stock(conn, oid)
            return_coupon(conn, order)
        else:
            conn.execute("UPDATE orders SET status='partially_refunded' WHERE id=?", (oid,))
        conn.execute("COMMIT")
        result = {
            "order_id": oid,
            "refunded_cents": amount,
            "total_refunded": refunded + amount,
            "full": full,
        }
    except HTTPException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return result


# ---------- 超时取消 ----------

@app.post("/internal/expire-orders")
def expire_orders():
    """取消超时未支付订单，生产由定时任务调用。"""
    conn = connect()
    expired = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM orders WHERE status='created' "
            "AND expire_at IS NOT NULL AND expire_at < datetime('now')"
        ).fetchall()
        for order in rows:
            conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order["id"],))
            release_stock(conn, order["id"])
            return_coupon(conn, order)
        conn.execute("COMMIT")
        expired = len(rows)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return {"expired": expired}


@app.on_event("startup")
def _startup():
    init_db()
