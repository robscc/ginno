"""Vibe Island notch 集成(vibeisland.app/docs/custom-agents.md)。

把 Ginno 会话生命周期报给本机的 Vibe Island 应用:每个事件一次子进程调用
``~/.vibe-island/bin/vibe-island-bridge --source ginno``,单向 best-effort、
2s 超时、永不阻塞或报错 agent(指南 Critical rules 3/4)。

开关在 Settings → Connectors → Vibe Island(connectors/builtin.py 的
VibeIslandConnector);关闭(默认)或 bridge 不存在时,本模块所有入口 no-op。
启用状态从 registry config 懒加载,所以 runtime 重启后开关仍然生效
(apply_config 只在 PATCH 时被调,不能依赖它做启动恢复)。

事件映射(指南 Step 3 的生命周期表 → Ginno 的既有事件收口):
    create_session            → SessionStart
    delete_session            → SessionEnd
    turn._run_stream          → UserPromptSubmit
    turn._run_resume          → PermissionResult(清掉「待批准」提醒)
    engine emit()/server_shared._push_session_event(单一收口,on_turn_event):
        tool.start            → PreToolUse
        tool.end              → PostToolUse(tool 名靠 start 时记的 id→name)
        permission.request    → Notification(permission_prompt)
        user.question         → Notification(permission_prompt, ask_user)
        version.propose       → Notification(permission_prompt)
        message.end           → Stop(last_assistant_message = 收尾文本)
        turn.stopped          → Stop(用户停 turn,卡片回 idle)
        error                 → StopFailure

子会话不入卡(指南「Not supported: sub-sessions」,每个 subagent 一张卡只会
是噪音):带 parent_session_id 或 type=subagent 的 session 全部事件静默跳过。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

SOURCE = "ginno"
_BRIDGE = Path(os.path.expanduser("~")) / ".vibe-island" / "bin" / "vibe-island-bridge"
_MANIFEST_DIR = Path(os.path.expanduser("~")) / ".vibe-island" / "integrations"
_TIMEOUT_S = 2.0
_QUEUE_MAX = 256

# 开关:None = 尚未从 registry config 懒加载。apply_config 直接 set_enabled。
_enabled: bool | None = None

# 串行发送队列 + 懒启动 worker:同一 loop 内天然保序(指南要求每 session
# 事件有序;单队列是它的超集)。
_queue: asyncio.Queue | None = None
_worker: asyncio.Task | None = None

# tool.start 的 id→name,给 tool.end(只有 id)补 PostToolUse 的 tool_name。
_tool_names: dict[str, dict[str, str]] = {}
# session→cwd 缓存(SessionStart 时定,事件全带 cwd——VI 重启后靠它复活卡片)。
_cwds: dict[str, str] = {}


# --------------------------------------------------------------------------- #
# 开关 / 环境
# --------------------------------------------------------------------------- #
def set_enabled(value: bool) -> None:
    """连接器 apply_config 的落点:开/关事件发送。"""
    global _enabled
    _enabled = bool(value)


def is_enabled() -> bool:
    """懒加载开关:首次调用读 registry config(settings.json connectors 键)。

    连接器未注册(测试环境 / 启动 seeding 前)一律视为关:read_config 对
    未知 id 的通用兜底是 {"enabled": True}(浏览器双轨的语义),照单全收
    会在 pytest 里把真事件发到用户的 Vibe Island。"""
    global _enabled
    if _enabled is None:
        try:
            from .connectors.registry import registry

            reg = registry()
            _enabled = (
                reg.get("vibe-island") is not None
                and bool(reg.read_config("vibe-island").get("enabled"))
            )
        except Exception:  # noqa: BLE001 — 读不到就当关着
            _enabled = False
    return _enabled


def bridge_available() -> bool:
    return os.access(str(_BRIDGE), os.X_OK)


def app_running() -> bool:
    """Vibe Island 应用是否在跑(状态上报用;事件本身不查——bridge 永不失败)。"""
    try:
        return subprocess.run(
            ["pgrep", "-x", "vibe-island"],
            capture_output=True, timeout=2,
        ).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def ensure_manifest() -> None:
    """写 ~/.vibe-island/integrations/ginno.json(指南 Step 5,唯一允许写
    ~/.vibe-island 的文件)。displayName 让卡片显示「Ginno」而不是「Ginno」
    的 source 名大写化;bundleIdentifier 供点击卡片时把 Ginno 带到前台。

    jumpRule:点击卡片时让 Vibe Island 打开 ginno://session/<id>,由桌面壳
    (Info.plist 注册 ginno scheme + lib.rs RunEvent::Opened → notify.rs
    deep_link_open_session)恢复窗口并导航到对应会话。{raw_session_id} 是
    指南占位符,替换成事件里发送的原始 session_id。
    ⚠️ 需要已安装并签名、注册了 ginno:// scheme 的构建:scheme 无人处理时
    macOS 会弹「没有应用能打开该 URL」,所以务必 make app 重装后再启用开关。"""
    payload = {
        "displayName": "Ginno",
        "bundleIdentifier": "io.ginno.desktop",
        "jumpRule": {
            "method": "urlScheme",
            "template": "ginno://session/{raw_session_id}",
        },
    }
    try:
        f = _MANIFEST_DIR / f"{SOURCE}.json"
        if f.exists():
            try:
                if json.loads(f.read_text() or "{}") == payload:
                    return
            except (json.JSONDecodeError, OSError):
                pass
        _MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(payload, indent=2) + "\n")
        _log.info("vibe_island_manifest_written path=%s", f)
    except OSError:
        _log.exception("vibe_island_manifest_failed")


# --------------------------------------------------------------------------- #
# 发送(worker 串行 spawn bridge,2s 超时,吞掉一切错误)
# --------------------------------------------------------------------------- #
def _send(payload: dict) -> None:
    """入队(同步入口,任何上下文可调)。队列满/无 loop 时静默丢弃——
    状态上报绝不值得报错,更不值得阻塞。"""
    if not is_enabled() or not bridge_available():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 不在事件循环里(不该发生):丢弃
    global _queue, _worker
    try:
        if _queue is None:
            _queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        _queue.put_nowait(payload)
        if _worker is None or _worker.done():
            _worker = loop.create_task(_drain())
    except (asyncio.QueueFull, Exception):  # noqa: BLE001 — 见 docstring
        pass


async def _drain() -> None:
    assert _queue is not None
    while True:
        payload = await _queue.get()
        try:
            proc = await asyncio.create_subprocess_exec(
                str(_BRIDGE), "--source", SOURCE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                # communicate 带超时:stdout/stderr 已 DEVNULL,不会管道阻塞。
                await asyncio.wait_for(
                    proc.communicate(json.dumps(payload, ensure_ascii=False).encode()),
                    timeout=_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                proc.kill()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — bridge 缺失/崩溃都只记日志
            _log.debug("vibe_island_send_failed event=%s", payload.get("hook_event_name"),
                       exc_info=True)


# --------------------------------------------------------------------------- #
# session 事实(cwd / 子会话过滤)
# --------------------------------------------------------------------------- #
def _session_entry(session_id: str) -> dict | None:
    try:
        from .server_shared import _SESSIONS

        entry = _SESSIONS.get(session_id)
        return entry if isinstance(entry, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _eligible(session_id: str) -> bool:
    """子会话(subagent/委托/团队成员,都带 parent 链)不入卡。条目不在内存
    (runtime 重启后的老会话)时放行:下一个事件带 cwd 会复活卡片,正是指南
    期望的行为。"""
    entry = _session_entry(session_id)
    if entry is None:
        return True
    return not entry.get("parent_session_id") and entry.get("type") != "subagent"


def _cwd_for(session_id: str) -> str:
    """卡片名 = 项目目录:primary folder 挂载优先(用户真正在干活的目录),
    回落 session 文件目录。"""
    cached = _cwds.get(session_id)
    if cached:
        return cached
    entry = _session_entry(session_id) or {}
    cwd = str(entry.get("primary_path") or entry.get("workspace") or Path.cwd())
    _cwds[session_id] = cwd
    return cwd


def _base(session_id: str, event: str) -> dict:
    # session_id 是 uuid4().hex——随机、不共用前缀(指南规则 2:相同前缀的
    # id 会把点击送到错误的 tab)。
    return {"hook_event_name": event, "session_id": session_id, "cwd": _cwd_for(session_id)}


# --------------------------------------------------------------------------- #
# 公开入口(全部同步、全部绝不抛)
# --------------------------------------------------------------------------- #
def session_started(session_id: str) -> None:
    """create_session 的落点:卡片出现(idle)。"""
    try:
        if not is_enabled() or not _eligible(session_id):
            return
        _cwds.pop(session_id, None)  # 新绑定,重算 cwd
        _send(_base(session_id, "SessionStart"))
        report_status()
    except Exception:  # noqa: BLE001
        _log.debug("vibe_island_session_started_failed", exc_info=True)


def session_ended(session_id: str) -> None:
    """delete_session 的落点:卡片移除。对从未入卡的 id 发也无害(VI 忽略)。"""
    try:
        if not is_enabled():
            return
        # SessionEnd 之后该 id 不许再发任何事件(指南规则 2):清缓存。
        _send(_base(session_id, "SessionEnd"))
        _cwds.pop(session_id, None)
        _tool_names.pop(session_id, None)
    except Exception:  # noqa: BLE001
        _log.debug("vibe_island_session_ended_failed", exc_info=True)


def user_prompt(session_id: str, text: str) -> None:
    """_run_stream 的落点:卡片进入 working,摘要 = 用户输入(mods 重写后、
    真正进 graph 的文本)。"""
    try:
        if not is_enabled() or not _eligible(session_id):
            return
        payload = _base(session_id, "UserPromptSubmit")
        if text:
            payload["prompt"] = text[:200]
        _send(payload)
    except Exception:  # noqa: BLE001
        _log.debug("vibe_island_user_prompt_failed", exc_info=True)


def permission_result(session_id: str) -> None:
    """_run_resume 的落点:用户已回答/批准,立刻清掉「待批准」提醒
    (后续 tool 事件/Stop 也会清,这里只是清得更早)。"""
    try:
        if not is_enabled() or not _eligible(session_id):
            return
        _send(_base(session_id, "PermissionResult"))
    except Exception:  # noqa: BLE001
        _log.debug("vibe_island_permission_result_failed", exc_info=True)


def on_turn_event(session_id: str, event: str, data: dict) -> None:
    """Ginno 事件收口(engine emit() / server_shared._push_session_event)
    → Vibe Island 事件。未知事件(message.delta、keepalive、notice、
    turn.state、steer.* 等)直接忽略;高频路径,映射前先过开关。"""
    try:
        if not is_enabled():
            return
        for payload in _map_event(session_id, event, data or {}):
            if _eligible(session_id):
                _send(payload)
    except Exception:  # noqa: BLE001
        _log.debug("vibe_island_on_turn_event_failed event=%s", event, exc_info=True)


def _map_event(session_id: str, event: str, data: dict) -> list[dict]:
    """纯映射(无副作用除 _tool_names 记账),便于单测。"""
    if event == "tool.start":
        name = str(data.get("name") or "")
        tid = data.get("id")
        if tid and name:
            names = _tool_names.setdefault(session_id, {})
            names[str(tid)] = name
            if len(names) > 512:  # 有界:长 turn 的 tool 调用不会撑爆内存
                names.pop(next(iter(names)))
        p = _base(session_id, "PreToolUse")
        if name:
            p["tool_name"] = name
        return [p]
    if event == "tool.end":
        tid = str(data.get("id") or "")
        name = (_tool_names.get(session_id) or {}).pop(tid, "")
        p = _base(session_id, "PostToolUse")
        if name:
            p["tool_name"] = name
        return [p]
    if event == "permission.request":
        p = _base(session_id, "Notification")
        p["notification_type"] = "permission_prompt"
        if data.get("tool"):
            p["tool_name"] = str(data["tool"])
        args = _small_tool_input(data.get("args"))
        if args is not None:
            p["tool_input"] = args
        return [p]
    if event == "user.question":
        # ask_user parked:等用户回答 = 指南语义里的「需要批准」提醒
        # (点击回到 Ginno;回答后 _run_resume 的 PermissionResult 清掉)。
        # 刘海端无法代答:bridge 单向,「answer questions from the notch」在
        # 指南里仍是 Planned——所以把选项列表也拼进 message,用户能在刘海
        # 先读到选项,点卡片回 Ginno 再选。
        p = _base(session_id, "Notification")
        p["notification_type"] = "permission_prompt"
        p["tool_name"] = "ask_user"
        msg = " ".join(
            str(data.get(k) or "") for k in ("header", "question")
        ).strip()
        opts: list[str] = []
        for o in data.get("options") or []:
            if isinstance(o, dict):  # 防御:规范化后应为纯 label 列表
                o = o.get("label") or o.get("text") or ""
            o = str(o).strip()
            if o:
                opts.append(o[:24])
        if opts:
            msg = (msg + "  " if msg else "") + " / ".join(opts[:5])
        if msg:
            p["message"] = msg[:200]
        return [p]
    if event == "version.propose":
        p = _base(session_id, "Notification")
        p["notification_type"] = "permission_prompt"
        p["tool_name"] = "version_propose"
        return [p]
    if event == "message.end":
        p = _base(session_id, "Stop")
        text = str(data.get("text") or "")
        if text:
            p["last_assistant_message"] = text[:200]
        return [p]
    if event == "turn.stopped":
        return [_base(session_id, "Stop")]
    if event == "error":
        p = _base(session_id, "StopFailure")
        text = str(data.get("message") or "")
        if text:
            p["last_assistant_message"] = text[:200]
        return [p]
    return []


def _small_tool_input(args: Any) -> dict | None:
    """tool_input 只带得动小对象(卡片只显示短摘要);大参数(文件内容等)
    直接省略——也顺带避免把用户数据摊到 notch 上。"""
    if not isinstance(args, dict) or not args:
        return None
    try:
        if len(json.dumps(args, ensure_ascii=False)) > 500:
            return None
    except (TypeError, ValueError):
        return None
    return args


# --------------------------------------------------------------------------- #
# 连接器状态上报(60s 冷却,SessionStart 顺带刷新)
# --------------------------------------------------------------------------- #
_last_status_at = 0.0


def report_status(force: bool = False) -> None:
    """把 bridge/应用探测结果报给连接器卡片。apply_config 强制刷;
    session_started 顺带刷(带冷却,pgrep 一次 ~5ms 也不值得每事件跑)。"""
    global _last_status_at
    try:
        import time

        now = time.monotonic()
        if not force and now - _last_status_at < 60.0:
            return
        _last_status_at = now
        from .connectors.registry import (
            STATUS_CONNECTED,
            STATUS_DISCONNECTED,
            STATUS_NOT_INSTALLED,
            registry,
        )

        reg = registry()
        if reg.get("vibe-island") is None:
            return
        enabled = is_enabled()
        if not bridge_available():
            reg.report("vibe-island", STATUS_NOT_INSTALLED,
                       "Vibe Island not found — install it from vibeisland.app "
                       "and open it once")
        elif not enabled:
            reg.report("vibe-island", STATUS_DISCONNECTED, "Off",
                       extra={"idle": True})
        elif not app_running():
            reg.report("vibe-island", STATUS_DISCONNECTED,
                       "Vibe Island is not running (events would be dropped)")
        else:
            reg.report("vibe-island", STATUS_CONNECTED,
                       "Sending session events to the notch")
    except Exception:  # noqa: BLE001 — 状态上报失败不影响事件发送
        _log.debug("vibe_island_report_status_failed", exc_info=True)
