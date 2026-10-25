# 缺陷扫描结果

`python scripts/fault_scan.py` 的输出。十一类缺陷逐个注入，看用例能不能抓住。

## 当前结果

```
缺陷                    结果      失败用例    通过用例
oversell              检出      2         78
no_idempotent         检出      1         79
no_release_stock      检出      2         78
float_money           检出      1         79
bad_state_machine     检出      2         78
no_qty_check          检出      6         74
no_authz              检出      1         79
wrong_status_code     检出      2         78
coupon_reuse          检出      2         78
no_split              检出      2         78
refund_over           检出      6         74

检出率：11/11 = 100%
```

判定方式：注入后套件跑红就是检出，仍全绿说明用例有盲区。

## 一开始没抓住的两类

第一版只有 8 类缺陷、检出 6/8，漏的是幂等和金额精度。

**幂等**：断言写成 `second.status_code in (200, 409)`，服务端把重复回调当错误抛 409 也算通过。
改成"必须返回 200 且 `duplicated=true`"才抓得住——幂等的正确表现是照常返回并标记重复，
不是把第二次回调当失败，网关会一直重试。

**金额精度**：原先用的商品单价都是整元（100.00），浮点累加不产生误差。
换成 5.01 元 × 5 件（应为 2505 分，浮点算出 2504）才暴露。整元价格永远测不出这类问题。

## 后加的三类

加了优惠券和退款之后补的：

- **coupon_reuse**：用券后不标记已用。用例 `test_coupon_cannot_be_used_twice` 抓
- **no_split**：多商品分摊按原价算，合计不等于实付。用例 `test_split_sums_to_payable` 抓
- **refund_over**：退款不校验累计上限。参数化用例 `test_exceed_remain_rejected` 抓，三个入参都命中

## 超卖：1 件库存卖出 3 单

`test_concurrent.py` 跑出来的，串行跑一万次也看不见。

最初的下单逻辑先 `SELECT stock` 再 `UPDATE stock`。10 个线程同时读到"库存 1"，
都判断够，都扣，成功 3 单，库存扣成 -2。

修法是合并成一条带条件的更新，放进同一个事务：

```sql
UPDATE products SET stock = stock - ? WHERE id = ? AND stock >= ?
```

`rowcount = 0` 说明库存被抢走，直接回滚。
