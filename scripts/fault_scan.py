"""逐个注入缺陷，统计用例检出率。"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT_BASE = 8200

FAULTS = [
    "oversell",
    "no_idempotent",
    "no_release_stock",
    "float_money",
    "bad_state_machine",
    "no_qty_check",
    "no_authz",
    "wrong_status_code",
    "coupon_reuse",
    "no_split",
    "refund_over",
]

SUMMARY = re.compile(r"(\d+) failed, (\d+) passed|(\d+) passed")


def wait_port(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def parse_result(output: str) -> tuple[int, int]:
    m = SUMMARY.search(output)
    if not m:
        return 0, 0
    if m.group(1):
        return int(m.group(1)), int(m.group(2))
    return 0, int(m.group(3))


def run_case(fault: str, port: int, keep_db: bool = False) -> dict:
    db = ROOT / f".fault_{fault}.db"
    if db.exists():
        db.unlink()

    env = os.environ.copy()
    env["SUT_DB"] = str(db)
    proc = subprocess.Popen(
        [PY, "-m", "sut.run", "--port", str(port), "--fault", fault, "--reset-db"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    result = {"fault": fault, "failed": 0, "passed": 0, "killed": False, "error": ""}
    try:
        if not wait_port(port):
            result["error"] = "服务未启动"
            return result

        env2 = env.copy()
        env2["SUT_BASE_URL"] = f"http://127.0.0.1:{port}"
        env2["SUT_DB"] = str(db)
        cp = subprocess.run(
            [PY, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "--tb=no"],
            cwd=ROOT,
            env=env2,
            capture_output=True,
            text=True,
            timeout=300,
        )
        failed, passed = parse_result(cp.stdout + cp.stderr)
        result["failed"] = failed
        result["passed"] = passed
        result["killed"] = failed > 0
    except subprocess.TimeoutExpired:
        result["error"] = "用例超时"
    finally:
        proc.kill()
        proc.wait(timeout=10)
        if not keep_db and db.exists():
            try:
                for suffix in ("", "-wal", "-shm"):
                    f = Path(str(db) + suffix)
                    if f.exists():
                        f.unlink()
            except OSError:
                pass

    return result


def main() -> int:
    targets = sys.argv[1:] or FAULTS
    rows = []

    for i, fault in enumerate(targets):
        port = PORT_BASE + i
        print(f"[{i + 1}/{len(targets)}] 注入 {fault} ...", end=" ", flush=True)
        row = run_case(fault, port)
        rows.append(row)
        if row["error"]:
            print(f"ERROR ({row['error']})")
        else:
            mark = "检出" if row["killed"] else "漏检"
            print(f"{mark}  failed={row['failed']} passed={row['passed']}")

    print("\n" + "=" * 62)
    print(f"{'缺陷':<22}{'结果':<8}{'失败用例':<10}{'通过用例'}")
    print("-" * 62)
    for r in rows:
        mark = "检出" if r["killed"] else "漏检"
        print(f"{r['fault']:<22}{mark:<8}{r['failed']:<10}{r['passed']}")

    killed = sum(1 for r in rows if r["killed"])
    total = len(rows)
    print("-" * 62)
    print(f"检出率：{killed}/{total} = {killed / total * 100:.0f}%")
    survived = [r["fault"] for r in rows if not r["killed"]]
    if survived:
        print(f"未被检出（需补用例）：{', '.join(survived)}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
