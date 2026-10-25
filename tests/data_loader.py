"""读 tests/data 下的 YAML 用例数据。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

DATA_DIR = Path(__file__).resolve().parent / "data"


@lru_cache(maxsize=32)
def load(name: str) -> dict:
    path = DATA_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"用例数据不存在: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def cases(name: str, group: str) -> list[dict]:
    data = load(name)
    if group not in data:
        raise KeyError(f"{name}.yaml 里没有 {group} 这一组")
    return data[group]
