"""启动被测服务，参数见 --help。"""
import argparse
import os
import sys

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--fault",
        default="",
        help="缺陷开关名，逗号分隔；all 表示全开；空表示全关",
    )
    parser.add_argument("--reset-db", action="store_true", help="启动前清库重建")
    parser.add_argument("--workers", type=int, default=1, help="进程数，压测对比用")
    args = parser.parse_args()

    if args.fault:
        if args.fault == "all":
            os.environ["FAULT_ALL"] = "1"
            print("[sut] 缺陷注入: ALL")
        else:
            for name in args.fault.split(","):
                os.environ[f"FAULT_{name.strip()}"] = "1"
            print(f"[sut] 缺陷注入: {args.fault}")

    if args.reset_db:
        from sut.app.db import init_db

        init_db(reset=True)
        print("[sut] 数据库已重建")

    uvicorn.run(
        "sut.app.main:app",
        host=args.host,
        port=args.port,
        log_level="warning",
        workers=args.workers,
    )


if __name__ == "__main__":
    sys.exit(main())
