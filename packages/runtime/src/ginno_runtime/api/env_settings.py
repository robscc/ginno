"""env 专用设置端点：每键 CAS 的 GET/PATCH + settings 健康与备份。

接口契约（前端对接文档见 /tmp/ginno-settings-integrity-contract.md）：

- ``GET  /api/settings/env``     — 每键视图：普通键回明文 value，敏感键
  只回元数据（set/kind/url 元信息）+ version token（与值内容无关的随机
  串，不是 hash——低熵秘密的裸 hash 可被字典攻击）。
- ``PATCH /api/settings/env``    — 全事务 CAS：body ``{updates: [...]}``；
  任一键版本冲突 → 409 + conflicts 清单，全部不落盘；同键重复 → 400。
- ``GET  /api/settings/health``  — 三态健康（ok/missing/corrupt）+ 错误
  详情，供损坏横幅。
- ``POST /api/settings/backup``  — 非破坏备份原字节到旁路文件（损坏恢复
  流程第一步；原文件绝不动）。

settings.json 损坏时：GET env/health 如实报告，PATCH 与所有写入口拒写
（409）保留原字节——修复只能由用户显式完成（手动编辑或 UI PUT 一份有效
文档），本 API 绝不自动覆盖。

损坏降级硬线（settings-integrity 复核补强，详见 settings_gate.py）：

- startup 检测到损坏时**不启动**执行/恢复型后台任务（scheduler 孤儿续跑、
  lastwords auto 续跑、plan watcher、MCP 连接、connector 注册、标题生成、
  tasks registry、team recovery——startup_tasks 的
  ``skip_when_settings_corrupt`` 名单）；
- 运行期由 ASGI 闸门兜底：除本模块端点 + ``/api/health`` + 静态资产外
  的一切 HTTP → 503 ``settings_corrupt_degraded``、一切 WebSocket →
  close(1013)；
- ``PUT /api/settings/repair`` **仅限当前仍是损坏态**：文件已被更新的
  动作修好时 → 409 ``settings_not_corrupt``（迟到的旧横幅快照不覆盖
  健康设置）；missing 态等同首次写；
- 修复成功后闸门自动解除，但 startup 已跳过的组件需重启补启——repair
  响应与 health（startup 曾降级时）都带 ``restart_required: true``。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import env_store
from .. import settings_gate
from .. import settings_store

router = APIRouter()

log = logging.getLogger(__name__)

_RECOVERY_HINT = (
    "settings.json is corrupt; writes are refused to preserve the original "
    "bytes. Back it up (POST /api/settings/backup), fix it manually or via "
    "the UI (PUT a valid document), then restart."
)


def corrupt_response(exc: settings_store.SettingsCorruptError, *, status: int = 409):
    """损坏拒写的统一错误体（供本模块与 api/config.py 复用）。"""
    return JSONResponse(
        status_code=status,
        content={
            "ok": False,
            "error": "settings_corrupt",
            "detail": exc.error or str(exc),
            "path": exc.path,
            "recovery": {
                "backup": "POST /api/settings/backup",
                "fix": "PUT /api/settings with a valid document (explicit user action)",
                "hint": _RECOVERY_HINT,
            },
        },
    )


@router.get("/api/settings/env")
async def get_env_settings() -> dict:
    return env_store.read_state()


@router.patch("/api/settings/env")
async def patch_env_settings(body: dict) -> dict:
    try:
        return env_store.apply_updates(body)
    except env_store.EnvUpdateRejected as e:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "invalid_request", "detail": e.error},
        )
    except env_store.EnvVersionConflict as e:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "version_conflict",
                "detail": "one or more keys changed since your read; "
                          "nothing was applied (all-or-nothing)",
                "conflicts": e.conflicts,
            },
        )
    except settings_store.SettingsCorruptError as e:
        return corrupt_response(e)
    except settings_store.SettingsWriteError as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "write_failed", "detail": str(e)},
        )


@router.put("/api/settings/repair")
async def repair_settings(body: dict) -> dict:
    """唯一的损坏态写通道：用户显式发起的修复。

    普通 PUT /api/settings 在损坏态一律 409（防盲目 round-trip 覆盖）；
    修复 = 用户在损坏横幅上确认「用这份文档替换」——服务端先自动备份
    原字节（settings.json.bak-*-pre-repair，非破坏）再落盘。body 必须
    是 JSON object。返回 restart_required 提示（hooks/权限等进程内缓存
    需重启或重读生效）。

    硬线（settings-integrity 复核补强）：**仅限当前仍是损坏态**。横幅
    打开时抓到的旧快照在文件已被手动修复/先到 repair 修好后到达 →
    409 ``settings_not_corrupt``（拒绝覆盖健康设置）；missing 态无文件
    可覆盖，等同首次写放行。
    """
    try:
        result = settings_store.repair(body)
    except settings_store.SettingsNotCorruptError as e:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "settings_not_corrupt",
                "detail": str(e),
                "path": e.path,
                "hint": (
                    "settings.json is no longer corrupt (fixed by a newer "
                    "action). Reload the page before editing; your stale "
                    "repair was NOT applied."
                ),
            },
        )
    except settings_store.SettingsWriteError as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "write_failed", "detail": str(e)},
        )
    return {
        "ok": True,
        "backup": str(result["backup"]) if result["backup"] else None,
        "restart_required": True,
    }


@router.get("/api/settings/health")
async def get_settings_health() -> dict:
    out = settings_store.health()
    # startup 曾因损坏降级（跳过过执行/恢复任务）→ 修复后这些组件不会
    # 自动补启，必须重启。横幅据此在 status=ok 后仍显示重启指引。
    if settings_gate.startup_degraded():
        out["restart_required"] = True
    return out


@router.post("/api/settings/backup")
async def backup_settings() -> dict:
    try:
        target = settings_store.backup()
    except FileNotFoundError:
        return {"ok": False, "error": "settings.json does not exist"}
    except OSError as e:
        return JSONResponse(
            status_code=500, content={"ok": False, "error": f"backup failed: {e}"}
        )
    return {"ok": True, "path": str(target)}
