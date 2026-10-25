"""本地跑一遍 CI 流程：起服务 → 等健康 → 跑用例 → 缺陷扫描 → 收尾。

和 GitHub Actions 里做的是同一套事，只是不依赖远端。
用独立端口和临时库，不碰开发中的那个 shop.db。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT = 8100


def wait_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def step(name: str) -> None:
    print(f"\n=== {name} ===")


def main() -> int:
    db = Path(tempfile.gettempdir()) / "qa_ci.db"
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db) + suffix)
        if f.exists():
            f.unlink()

    env = os.environ.copy()
    env["SUT_DB"] = str(db)
    env["SUT_BASE_URL"] = f"http://127.0.0.1:{PORT}"

    step("启动被测服务")
    proc = subprocess.Popen(
        [PY, "-m", "sut.run", "--port", str(PORT), "--reset-db"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    rc = 0
    try:
        if not wait_port(PORT):
            print("服务没起来", file=sys.stderr)
            return 2

        step("跑用例")
        r = subprocess.run(
            [PY, "-m", "pytest", "-q", "--html=reports/report.html", "--self-contained-html"],
            cwd=ROOT,
            env=env,
        )
        if r.returncode != 0:
            rc = 1

        step("缺陷扫描")
        r = subprocess.run([PY, "scripts/fault_scan.py"], cwd=ROOT, env=env)
        if r.returncode != 0:
            rc = 1
    finally:
        step("收尾")
        proc.kill()
        proc.wait(timeout=10)
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(db) + suffix)
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass
        print("服务已停止，临时库已清理")

    print(f"\n结果：{'通过' if rc == 0 else '失败'}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
