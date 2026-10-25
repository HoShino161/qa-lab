"""请求封装。"""
from __future__ import annotations

import time
from typing import Any

import requests

from tests.logger import get_logger


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        # 沙箱/公司内网常挂 HTTP_PROXY，连本机服务会被代理劫持，这里强制直连
        self.session.trust_env = False
        self.last_exchange: dict[str, Any] = {}
        self.log = get_logger()

    def request(self, method: str, path: str, *, token: str | None = None, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        headers = dict(kwargs.pop("headers", {}) or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"

        started = time.perf_counter()
        resp = self.session.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
        elapsed_ms = (time.perf_counter() - started) * 1000

        self.last_exchange = {
            "method": method,
            "url": url,
            "request_body": kwargs.get("json"),
            "status": resp.status_code,
            "response_body": _safe_json(resp),
            "elapsed_ms": round(elapsed_ms, 2),
        }
        self._write_log(resp, elapsed_ms)
        return resp

    def _write_log(self, resp: requests.Response, elapsed_ms: float) -> None:
        body = _safe_json(resp)
        line = (
            f"{resp.request.method} {resp.request.path_url} -> {resp.status_code} "
            f"{elapsed_ms:.1f}ms"
        )
        if resp.status_code >= 400:
            self.log.warning("%s body=%s resp=%s", line, _short(resp.request.body), _short(body))
        else:
            self.log.info("%s", line)

    def get(self, path: str, **kwargs) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> requests.Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> requests.Response:
        return self.request("DELETE", path, **kwargs)

    def close(self) -> None:
        self.session.close()


def _safe_json(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return resp.text[:500]


def _short(value, limit: int = 400) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", "ignore")
    else:
        text = str(value)
    return text[:limit] + ("…" if len(text) > limit else "")


def describe(resp: requests.Response) -> str:
    """失败时拼一段能直接贴进缺陷单的现场描述。"""
    body = _safe_json(resp)
    return f"status={resp.status_code} body={body} url={resp.request.method} {resp.url}"
