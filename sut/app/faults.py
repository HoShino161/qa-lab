"""缺陷注入开关，用环境变量控制。全关时是正确的实现。"""
import os

# 开关名 -> 注入什么缺陷
FAULT_SPEC = {
    "oversell": "下单不校验库存",
    "no_idempotent": "支付回调不校验幂等键",
    "no_release_stock": "取消订单不释放库存",
    "float_money": "金额用浮点计算",
    "bad_state_machine": "已取消订单允许再次取消",
    "no_qty_check": "允许数量 <= 0 下单",
    "no_authz": "查订单不校验归属",
    "wrong_status_code": "库存不足返回 200 而非 4xx",
    "coupon_reuse": "用券后不标记已用，同一张券能反复抵扣",
    "no_split": "优惠按原价分摊，不按比例",
    "refund_over": "退款不校验累计上限，能退超额",
}


def on(name: str) -> bool:
    if os.getenv("FAULT_ALL", "0") == "1":
        return True
    return os.getenv(f"FAULT_{name}", "0") == "1"


def enabled_faults() -> list[str]:
    return [k for k in FAULT_SPEC if on(k)]
