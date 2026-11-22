"""损坏降级闸门（settings-integrity 硬线，2026-11-22 复核补强）。

背景：原实现的损坏防线只有两条——hooks 退空、权限默认 ask——但
startup_tasks 仍会启动 scheduler（孤儿 running 行自动续跑）、lastwords
recover（auto headless 续跑）、plan watcher、MCP 连接、connector 注册、
tasks registry 等执行/恢复型后台任务；UI/WS/渠道入口也全部可用。损坏的
settings.json 不可信（模型/provider/权限/hooks 配置全部读不出或读到垃圾），
在其上「照常营业」等于放大事故。

本模块提供两层硬线（都**动态**检查文件当前状态，repair 落盘即自动解除，
不需要重启来关闸）：

1. **ASGI 闸门** :class:`SettingsIntegrityGate`（server.py 挂载，位于
   OriginGuard 内层）：损坏期间除白名单外的一切 HTTP → 503
   ``settings_corrupt_degraded``、一切 WebSocket → close(1013)。
   白名单只有「受限恢复 UI」需要的面：
   - ``GET/HEAD /api/health`` —— 壳判活/看门狗靠它，拦了会被当 sidecar
     死掉反复重启；
   - ``/api/settings`` 前缀全部端点 —— 恢复 UI 的读写面；写操作由
     settings_store 层统一 409 拒绝（防御纵深第二层仍在，契约 §6 的
     409 ``settings_corrupt`` 语义不变），repair 是唯一放行的写通道；
   - 非 ``/api/`` 路径 —— Next 静态导出/页面（横幅所在的前端 app 必须
     能加载）；但其中**功能性端点**照拒（``/mcp`` MCP JSON-RPC、
     ``/a2a*``、``/docs``/``/redoc``/``/openapi.json``——见
     :data:`_BLOCKED_ROOT_SEGMENTS`）。
2. **startup 降级标记**：startup_tasks 在检测到损坏并跳过执行/恢复型
   任务时调用 :func:`note_startup_degraded`；此后
   ``GET /api/settings/health`` 附加 ``restart_required: true`` ——即使
   文件已被 repair 修好，scheduler/hooks 等进程内组件也不会自动补启，
   必须重启才回到全功能态（repair 响应同样带该提示）。

不在本层做的事：startup 任务表的跳过分派（startup_tasks.py 的
``skip_when_settings_corrupt`` 字段）、损坏拒写（settings_store.py）。
"""

from __future__ import annotations

import logging

from . import settings_store

log = logging.getLogger(__name__)

#: 本进程 startup 时是否检测到损坏（跳过过执行/恢复型任务）。
#: 每次 lifespan startup 由 startup_tasks 重置——单测同进程复用 app 时
#: 不残留上一个用例的状态。
_STARTUP_DEGRADED = False


def note_startup_degraded() -> None:
    global _STARTUP_DEGRADED
    _STARTUP_DEGRADED = True


def reset_startup_degraded() -> None:
    global _STARTUP_DEGRADED
    _STARTUP_DEGRADED = False


def startup_degraded() -> bool:
    return _STARTUP_DEGRADED


def is_degraded() -> bool:
    """当前是否处于损坏降级态（动态读盘——repair 落盘立即解除）。"""
    return settings_store.load_doc().corrupt


#: 非 /api 路径中的**功能性端点**根段（损坏期间照拒——它们是「实际工作」
#: 入口，不是静态资产）：/mcp（MCP JSON-RPC，可驱动工具执行）、/a2a*
#: （A2A 委派端点——当前以独立 node 进程形态存在，若挂进主 app 也被
#: 覆盖）、/docs|/redoc|/openapi.json（FastAPI 文档面，非恢复 UI 所需）。
#: 其余非 /api 路径（/、/settings、/_next/…、*.txt RSC payload）是
#: Next 静态导出——受限恢复 UI（横幅）所在，放行。
_BLOCKED_ROOT_SEGMENTS = frozenset(
    {"mcp", "a2a", "docs", "redoc", "openapi.json"}
)


def _allowed_during_degradation(scope: dict) -> bool:
    """白名单：受限恢复 UI 所需的最小面（其余全拒）。

    WebSocket 一律不放行——恢复流程（health/backup/repair 轮询）全部是
    HTTP，任何 WS 通道（turn/schedule/connectors/extension）都是「实际
    工作」的入口。
    """
    if scope.get("type") == "websocket":
        return False
    path = scope.get("path", "")
    if not path.startswith("/api/"):
        seg = path.lstrip("/").split("/", 1)[0]
        return seg not in _BLOCKED_ROOT_SEGMENTS
    if scope.get("method", "GET").upper() == "OPTIONS":
        return True  # CORS 预检无害（OriginGuard 在外层已做真正的源校验）
    if path == "/api/health":
        return True  # 壳判活/看门狗（GET/HEAD）
    if path == "/api/settings" or path.startswith("/api/settings/"):
        # 恢复 UI 读写面：写操作由 settings_store 统一 409 拒绝
        # （repair 是唯一放行的写通道）。
        return True
    return False


class SettingsIntegrityGate:
    """Pure-ASGI 损坏闸门（同 server.py RequestLocaleMiddleware 模式）。

    损坏期间：非白名单 HTTP → 503 JSON（带 recovery 指引）；WebSocket →
    在 accept 前 ``websocket.close(1013)``（客户端看到升级被拒）。
    每个请求动态判态（:func:`is_degraded`），repair 成功后立即恢复放行
    ——但 startup 已跳过的后台组件要重启才回来（见模块 docstring 第 2 层）。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") in ("http", "websocket")
            and is_degraded()
            and not _allowed_during_degradation(scope)
        ):
            path = scope.get("path", "")
            log.warning(
                "settings_corrupt_gate_blocked type=%s path=%s",
                scope.get("type"), path,
            )
            if scope["type"] == "http":
                from starlette.responses import JSONResponse

                resp = JSONResponse(
                    status_code=503,
                    content={
                        "ok": False,
                        "error": "settings_corrupt_degraded",
                        "detail": (
                            "settings.json is corrupt; the runtime is in "
                            "recovery-only mode. Actual work (turns, schedules, "
                            "channels, connectors) is refused until the file "
                            "is repaired."
                        ),
                        "recovery": {
                            "health": "GET /api/settings/health",
                            "backup": "POST /api/settings/backup",
                            "fix": "PUT /api/settings/repair",
                        },
                    },
                )
                await resp(scope, receive, send)
                return
            # websocket: accept 前直接关闭（1013 = try again later）
            await send({
                "type": "websocket.close",
                "code": 1013,
                "reason": "settings corrupt; recovery-only mode",
            })
            return
        await self.app(scope, receive, send)
