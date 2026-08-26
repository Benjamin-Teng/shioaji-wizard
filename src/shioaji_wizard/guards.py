"""桌面 app middleware：本機來源守衛＋回應快取修正。

原樣移植自 fcn-pricing 的 ``fcn_report.app.routes`` 對應段落（``_origin_guard``
約 L1347、``_cache_control`` 約 L1418），但本專案**只有桌面模式**（無雲端／
多租戶），故只保留該檔案桌面分支的邏輯：Host 非本機一律 403；副作用方法
（非 GET/HEAD/OPTIONS）若帶 Origin 且非本機同樣 403（擋 localhost-CSRF）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _extract_host(value: str) -> str:
    """由 Host（``host:port``）或 Origin（``scheme://host:port``）header 取出
    純 host（去 scheme／path／port，處理 IPv6 中括號）。"""
    v = value.strip()
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]
    if v.startswith("["):  # IPv6 literal，如 [::1]:8765
        return v[1 : v.index("]")] if "]" in v else v.strip("[]")
    if ":" in v:
        v = v.rsplit(":", 1)[0]
    return v


def _host_is_local(value: str | None) -> bool:
    """Host/Origin header 是否指向本機（127.0.0.1／localhost／::1）。None／空
    → False。用於擋 DNS-rebinding（Host 非本機）與跨來源副作用請求（Origin
    非本機）。"""
    if not value:
        return False
    return _extract_host(value) in _LOCAL_HOSTS


def install_guards(app: FastAPI) -> None:
    if getattr(app.state, "guards_installed", False):
        return  # 已掛過（server.py 建 app 時就掛；desktop.serve 再呼叫是 no-op）
    app.state.guards_installed = True
    _install_guards(app)


def _install_guards(app: FastAPI) -> None:
    """把 ``_origin_guard``／``_cache_control`` 掛到 ``app`` 上。

    註冊順序講究：Starlette 的 user_middleware 疊法是後註冊者最外層（見
    ``Router.build_middleware_stack``），``_cache_control`` 要包住整個鏈
    （連 ``_origin_guard`` 提早短路的 403 也要蓋到 no-store），故必須排在
    ``_origin_guard`` **之後**註冊。"""

    @app.middleware("http")
    async def _origin_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """僅接受本機請求（Host 非本機 403；副作用方法且 Origin 非本機
        403，擋 localhost-CSRF）。"""
        host = request.headers.get("host")
        if not _host_is_local(host):
            return JSONResponse({"detail": "非本機請求已拒絕"}, status_code=403)
        if request.method not in _SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and not _host_is_local(origin):
                return JSONResponse({"detail": "跨來源請求已拒絕"}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def _cache_control(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """回應快取修正：HTML／JSON 全靠瀏覽器 heuristic freshness 會讓部署後
        看不到新版，一律加 ``Cache-Control: no-store``，除 ``/static/*``。"""
        response = await call_next(request)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response
