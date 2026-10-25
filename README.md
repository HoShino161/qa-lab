# qa-lab

电商订单服务的接口自动化测试。被测服务和测试代码在同一个仓库，19 个接口覆盖库存扣减、订单状态机、支付幂等、优惠券分摊、退款、超时取消、金额精度、越权。

## 怎么跑

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt

python -m sut.run --reset-db     # 起被测服务，默认 8000 端口
pytest                            # 另开一个窗口跑用例

python scripts/ci.py              # 一键：起服务 → 跑用例 → 缺陷扫描 → 收尾
```

地址从环境变量 `SUT_BASE_URL` 读，默认 `http://127.0.0.1:8000`。

## 用例

80 条，分七个文件：

| 文件 | 覆盖 |
|---|---|
| `test_order.py` | 下单、库存扣减、取消释放库存、状态机、越权、金额精度 |
| `test_cart.py` | 加购、合并、隔离、下单清空 |
| `test_coupon.py` | 领券、门槛、抵扣、多商品分摊、取消退券 |
| `test_payment.py` | 支付回调、幂等重放、金额不符 |
| `test_refund.py` | 全额/部分退款、累计上限、库存与券回退 |
| `test_expire.py` | 超时未支付取消、幂等 |
| `test_concurrent.py` | 10 线程抢 1 件库存；同一用户并发下单 |

断言分两层：接口返回 + 直查数据库。库存相关用例用 `fresh_product` 自带商品，不碰共享数据。用例参数在 `tests/data/*.yaml`。

## 缺陷注入

每个开关把一处正确逻辑替换成典型错误，用来验证用例抓不抓得住：

```bash
python -m sut.run --fault oversell    # 只注入超卖
python -m sut.run --fault all         # 全开
python scripts/fault_scan.py          # 逐个注入，统计检出率
```

十一类缺陷当前全部能被用例抓到。结果和一条并发缺陷的定位过程见 [docs/defect-scan.md](docs/defect-scan.md)。

## 目录

```
sut/                被测服务
  app/main.py       全部接口
  app/db.py         建表与种子数据
  app/faults.py     缺陷开关
  run.py            启动入口
tests/
  conftest.py       夹具：用户、商品、数据库连接
  api_client.py     请求封装
  assertions.py     统一断言
  logger.py         请求日志
  data_loader.py    读 YAML 用例数据
  data/             用例参数
  test_*.py         用例
scripts/
  fault_scan.py     缺陷注入扫描
  ci.py             本地 CI：起服务、跑用例、扫描
docs/
  defect-scan.md    缺陷注入结果
```

## 已知限制

- SQLite 单文件，多进程写会排队。想压高并发得换库
