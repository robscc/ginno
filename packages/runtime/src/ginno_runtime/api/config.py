"""Settings/config endpoints: general settings, model providers, agents,
MCP server config, and skills management."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from threading import Lock

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ginno_runtime import jsonx as json

from .. import agents as agents_reg
from .. import env_store
from .. import paths
from .. import providers as prov_mod
from .. import server_shared as shared
from .. import settings_store
from ..api import env_settings as env_settings_api
from ..agents.registry import AgentModelBindingError
from ..lang import t
from ..mcp.registry import MCPRegistry
from ..model_tags import codex_efforts, inherit_model_effort, resolve_model_effort
from ..server_shared import _SESSIONS, _push_global_event
from ..session_meta import _session_meta_list
from ..skills.loader import SkillLoader

router = APIRouter()

log = logging.getLogger(__name__)


# ---- skills ----

# HTTP readers and writers share the lock now that file operations run in workers.
_SKILL_FILES_LOCK = Lock()


@router.get("/api/skills")
def list_skills(project_slug: str | None = None) -> list[dict]:
    with _SKILL_FILES_LOCK:
        skills = SkillLoader(project_slug=project_slug).load()
    return [
        {
            "name": s.name,
            "description": s.description,
            "trigger": s.trigger,
            "tools": s.allowed_tools,
            "builtin": s.builtin,
        }
        for s in skills
    ]


@router.get("/api/skills/{name}/body")
def get_skill_body(name: str, project_slug: str | None = None) -> dict:
    with _SKILL_FILES_LOCK:
        s = SkillLoader(project_slug=project_slug).get(name)
    return {"ok": bool(s), "body": s.body if s else ""}


@router.post("/api/skills")
async def create_skill_endpoint(data: dict) -> dict:
    name = (data.get("name") or "").strip()
    body = data.get("body") or ""
    if not name:
        return {"ok": False, "error": "name required"}
    def write_skill() -> None:
        with _SKILL_FILES_LOCK:
            d = paths.global_skills_dir() / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(body, encoding="utf-8")

    await run_in_threadpool(write_skill)
    await _push_global_event("skills.changed", {})
    return {"ok": True}


@router.delete("/api/skills/{name}")
async def delete_skill_endpoint(name: str) -> dict:
    import shutil

    def delete_skill() -> dict:
        with _SKILL_FILES_LOCK:
            d = paths.global_skills_dir() / name
            if d.exists():
                shutil.rmtree(d)
                return {"ok": True}
            s = SkillLoader().get(name)
            if s and s.builtin:
                return {"ok": False, "error": "builtin skill cannot be deleted"}
            return {"ok": False}

    result = await run_in_threadpool(delete_skill)
    if result["ok"]:
        await _push_global_event("skills.changed", {})
    return result


@router.post("/api/skills/import-dir")
async def import_skills_dir(data: dict) -> dict:
    """Import skills from a local directory (e.g. another agent's skills folder).

    Each sub-directory containing a ``SKILL.md`` (or lowercase ``skill.md``) is
    imported as one skill; the whole sub-directory (scripts, reference docs,
    mcp-config, etc.) is copied so script-backed skills keep working. If *path*
    itself is a single skill directory, only that one is imported. Existing
    skills are skipped unless ``overwrite`` is true.

    Shares its implementation with the agent-side ``install_skills`` tool
    (:mod:`ginno_runtime.skills.installer`).
    """
    from ..skills.installer import import_skills_from_dir

    def import_skills() -> dict:
        with _SKILL_FILES_LOCK:
            return import_skills_from_dir(
                (data or {}).get("path", ""),
                overwrite=bool((data or {}).get("overwrite", False)),
            )

    result = await run_in_threadpool(import_skills)
    if result.get("ok") and result.get("imported"):
        await _push_global_event("skills.changed", {})
    return result


# ---- mcp ----

# Serialize the REST config snapshot with its existing in-place write.
_MCP_CONFIG_LOCK = asyncio.Lock()


@router.get("/api/mcp")
async def list_mcp(tools: bool = False) -> dict:
    """``?tools=1`` 才返回逐服务器明细（status[].disabledTools/toolDetails）。

    前端 5s 轮询走不带参的轻负载（status 只有 name/connected/tools/error/
    enabled/connectedAt），进工具 tab / 详情视图才带参取明细。
    """
    if not shared._mcp:
        return {"servers": [], "tools": [], "failed": [], "status": []}
    reg = shared._mcp
    # Lazy healing (2026-08-10 incident): a startup-time DNS/network blip
    # used to leave MCP dead until a manual reload or app restart. The UI
    # polls this endpoint, so opportunistically re-attempt failed servers
    # here — cooldown + busy-guard live inside retry_failed(); the task is
    # fire-and-forget so a hung server can never stall the settings page.
    if reg.has_pending_failures():
        shared.spawn_bg(reg.retry_failed())
    return {
        "servers": list(reg.ensure_loaded().keys()),
        "tools": reg.list_tools(),
        "failed": reg.failed_servers,
        # 逐服务器连接状态（设置页展示）：基础字段 + enabled/connectedAt，
        # ?tools=1 再追加 disabledTools/toolDetails（服务器/工具级开关契约）
        "status": reg.status(include_tools=tools),
    }


def _read_mcp_config() -> dict:
    p = paths.mcp_config_path()
    if not p.exists():
        return {"mcpServers": {}}
    try:
        return json.loads(p.read_text() or '{"mcpServers": {}}')
    except json.JSONDecodeError as e:
        # 损坏文件不能伪装成空配置：前端会把「空列表」当真值源做读改写，
        # 任何一次 PUT（toggle / 添加 / 导入）都会把用户的原配置整个覆盖
        # 掉。带上 parseError 让设置页锁写并横幅报警（Advanced 全文覆盖
        # 保存仍是修复通道）。
        return {"mcpServers": {}, "parseError": f"{type(e).__name__}: {e}"}


@router.get("/api/mcp/config")
async def get_mcp_config_endpoint() -> dict:
    async with _MCP_CONFIG_LOCK:
        return await asyncio.to_thread(_read_mcp_config)


@router.put("/api/mcp")
async def put_mcp_endpoint(data: dict) -> dict:
    async with _MCP_CONFIG_LOCK:
        paths.mcp_config_path().write_text(json.dumps(data, indent=2, ensure_ascii=False))
    # 落盘后热更新运行中 registry 的内存 configs（2026-10-09 开关无响应修复）。
    # 此前只写文件：status() 停留在 load() 时刻的旧 enabled，5s 轮询把前端
    # 乐观翻转的开关翻回去，reconnect?server= 也读旧配置而无法逐出已禁用的
    # 服务器。sync_configs 只热同步开关字段（enabled/disabled_tools）并驱离
    # disabled 服务器；连接参数变更仍由前端走 /api/mcp/reload 全量重建。
    if shared._mcp:
        try:
            shared._mcp.sync_configs(data)
        except Exception:
            log.exception("mcp sync_configs after PUT failed (file is written)")
    return {"ok": True}


@router.post("/api/mcp/test")
async def test_mcp_endpoint(body: dict) -> dict:
    """向导「测试连接」的真试连（P1，mcp-server-ui-design §4.4）：对未保存
    的 draft entry 建立一次性连接（initialize + tools/list 即拆），不落盘、
    不碰 registry。HTTP 200 恒定，ok:false 带 provider 原始错误全文——
    接口错误永远亮给前端（2026-10-27 决策）。"""
    from ..mcp.registry import probe_mcp_entry

    entry = (body or {}).get("entry")
    if not isinstance(entry, dict) or not entry:
        return {"ok": False, "error": "body must carry {entry: {...}}"}
    to = body.get("timeout")
    try:
        result = await probe_mcp_entry(entry, float(to) if to else None)
    except Exception as e:  # noqa: BLE001 — probe 内部已兜，这里防签名外事故
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return result


@router.post("/api/mcp/reload")
async def reload_mcp_endpoint() -> dict:
    if shared._mcp:
        await shared._mcp.close_all()
    shared._mcp = MCPRegistry()
    shared._mcp.load()
    await shared._mcp.connect_all()
    return {"ok": True, "servers": list(shared._mcp.ensure_loaded().keys())}


@router.post("/api/mcp/reconnect")
async def reconnect_mcp_endpoint(server: str | None = None) -> dict:
    """重试未连上的服务器，不动已连接的（连接是幂等的：只补 pending）。

    与 reload 的区别：reload 重建整个 registry（会断开在用连接、让会话里
    缓存的工具包装器全部失效）；reconnect 只补缺口，适合设置页的重试按钮。

    可选 ``?server=<name>`` 只重试指定服务器（设置页的行级重试按钮）：
    同样不重建 registry、保持冷却簿记语义；对已连接/已禁用的名字是幂等
    no-op，未配置的名字返回 ok:false。
    """
    if not shared._mcp:
        return {"ok": True, "status": []}
    only = None
    if server is not None:
        if server not in shared._mcp.ensure_loaded():
            return {"ok": False, "error": f"unknown mcp server: {server}"}
        only = {server}
    await shared._mcp.connect_all(only=only)
    return {"ok": True, "status": shared._mcp.status()}


# ---- settings (general) ----


@router.get("/api/settings")
def get_settings() -> dict:
    """三态呈现：missing → ``{}``；ok → 掩码后的文档；corrupt →
    ``{"parseError": ...}``（与 mcp config 端点同一约定）。

    corrupt 不得伪装成空文档返回——前端会把「空」当真值源做读改写，
    一次 PUT 就洗掉用户原字节。parseError 键本身就是「锁写 + 横幅报警」
    的信号；PUT 侧会剥掉该键。
    """
    doc = settings_store.load_doc()
    if doc.corrupt:
        return {"parseError": doc.error or "settings.json is corrupt"}
    if doc.status == settings_store.STATUS_MISSING:
        return {}
    # SEC-01: never hand provider keys back in plaintext. PUT restores them.
    return prov_mod.mask_settings(doc.data)


@router.put("/api/settings")
async def put_settings(data: dict) -> dict:
    prev_use_proxy = prov_mod.use_system_proxy()  # reads the still-on-disk value
    # 损坏拒写（settings-integrity）：previous 必须来自有效的在盘文档，
    # 否则掩码无法还原 + 原字节被覆盖。corrupt → 409，保留原字节。
    doc = settings_store.load_doc()
    if doc.corrupt:
        return env_settings_api.corrupt_response(
            settings_store.SettingsCorruptError(
                "settings.json is corrupt; PUT refused",
                error=doc.error,
                path=str(doc.path),
            )
        )
    previous = doc.data if doc.status == settings_store.STATUS_OK else {}
    # GET 的损坏标记键不落盘（前端若原样回传）
    if isinstance(data, dict):
        data.pop("parseError", None)
    # Undo the GET masking before writing: callers round-trip the whole
    # document (e.g. the proxy toggle), so a masked key must resolve back to
    # the stored secret instead of being persisted as "••••1234".
    data = prov_mod.unmask_settings(data, previous)
    # 2026-10-29 review 定稿：残留掩码（草稿引用了存储里不存在的条目）=
    # 「未修改」占位，置空忽略而非报错；绝不把 •••• 写进 settings.json
    # ——掩码落盘后，聊天/分类器等真实流量会带着它出网，在 httpx ASCII
    # 编码处炸 UnicodeEncodeError（2026-10-24 智谱 key 事故同型）。
    cleared = prov_mod.clear_masked_leaves(data)
    if cleared:
        log.warning("settings_put_mask_placeholder_ignored fields=%s",
                     ",".join(cleared[:10]))
    try:
        settings_store.atomic_write(data)
    except settings_store.SettingsCorruptError as e:
        # 竞态窗口内损坏（读取后、落盘前被外部改坏）
        return env_settings_api.corrupt_response(e)
    except settings_store.SettingsWriteError as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "write_failed", "detail": str(e)},
        )
    # env 键集合可能随整文档变化：对齐每键 CAS 版本 token（env 未变则
    # token 保留，打开中的编辑器不失效）。
    env_store.after_full_document_write(data)
    new_use_proxy = prov_mod.use_system_proxy(data)
    if new_use_proxy != prev_use_proxy:
        # httpx clients freeze their proxy map at init — swap the lookup,
        # then drop every cache still holding clients built under the old
        # mode (apply_system_proxy also clears langchain_anthropic's shared
        # lru_cache'd clients).
        prov_mod.apply_system_proxy(new_use_proxy)
        _SESSIONS.clear()
    return {"ok": True}


# ---- model configs (Settings → 模型 API, v2) ----


def _agent_lookup(agent_id: str | None):
    return agents_reg.get_agent(agent_id) if agent_id else None


def _read_settings_file() -> dict:
    """宽容读（缺省/损坏 → {}）：消费型路径沿用；写路径必须走 settings_store。"""
    return settings_store.read_settings()


@router.get("/api/model_configs")
def get_model_configs() -> dict:
    settings = _read_settings_file()
    configs = prov_mod.load_configs(settings)
    # SEC-01: masked on read; PUT/verify/list_models unmask before using them.
    for c in configs:
        if c.get("api_key"):
            c["api_key"] = prov_mod.mask_secret(c["api_key"])
    return {
        "configs": configs,
        "default_config": prov_mod.get_default_config(settings),
    }


class PutModelConfigsRequest(BaseModel):
    configs: list
    default_config: str | None = None


def _config_references(config_id: str) -> dict:
    """Who still points at a config: agents (by provider id OR by a per-type
    sub-agent model binding) + default flag.

    子代理绑定也要算(2026-10-23):只探 ``agent.provider`` 会漏掉
    ``subagent_models[type].provider``,配置被删后那些绑定变成悬空引用,
    spawn 子代理时才炸。命中子代理绑定时点名是哪几个类型——否则用户翻
    agent 的 provider 下拉找不到这个配置,等于没说清原因。
    """
    settings = _read_settings_file()
    current_default = settings.get("default_config") or settings.get("default_provider")
    agent_refs: list[dict] = []
    for a in agents_reg.list_agents():
        subs = [
            type_name
            for type_name, binding in (getattr(a, "subagent_models", None) or {}).items()
            if (binding or {}).get("provider") == config_id
        ]
        if a.provider != config_id and not subs:
            continue
        ref = {"id": a.id, "name": a.name}
        if subs:
            ref["subagents"] = subs
        agent_refs.append(ref)
    return {
        "agents": agent_refs,
        "is_default": current_default == config_id,
    }


def _guard_config_removal(stored_ids: set[str], new_ids: set[str], new_default: str | None) -> None:
    """Deletion guard (decision: 删除被引用配置 → 400 带引用清单).

    A config that disappears in a full-array PUT may not be referenced by any
    agent, nor be the current default unless the request also moves the
    default elsewhere.
    """
    blocked: dict[str, dict] = {}
    for rid in sorted(stored_ids - new_ids):
        refs = _config_references(rid)
        if refs["agents"] or (refs["is_default"] and not new_default):
            blocked[rid] = refs
    if blocked:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "The following configs are still referenced and cannot be deleted: "
                + ", ".join(blocked.keys()),
                "references": blocked,
            },
        )
    return None


@router.put("/api/model_configs")
async def put_model_configs(req: PutModelConfigsRequest):
    stored = {c["id"]: c for c in prov_mod.load_configs()}
    # SEC-01: the UI edits a masked config; restore masked api_keys from the
    # stored copy before validation/save. 掩码 = 「未修改」占位（2026-10-29
    # review 定稿）：能还原就还原；还原不了（id 不在存储里，如复制配置换
    # 了 id）置空忽略，绝不把 •••• 落盘、不报错打断保存。
    raw_cfgs = []
    for c in req.configs:
        if isinstance(c, dict):
            prev = stored.get(str(c.get("id") or "").strip()) or {}
            c = {"model_tags": prev.get("model_tags", {}), **c}
        else:
            return JSONResponse(
                status_code=400, content={"ok": False, "error": "Each config must be an object"},
            )
        if prov_mod.is_masked(c.get("api_key")):
            if not prev.get("api_key"):
                log.warning(
                    "model_configs_put_mask_placeholder_ignored id=%s", c.get("id")
                )
            c["api_key"] = prev.get("api_key") or ""
        raw_cfgs.append(c)
    try:
        normalized = prov_mod.validate_configs(raw_cfgs)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})
    new_ids = {c["id"] for c in normalized}
    default = req.default_config
    if default is not None and default not in new_ids:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"default_config {default!r} is not in configs"},
        )
    if default is not None and any(
        c["id"] == default and c.get("kind") == "asr" for c in normalized
    ):
        # 语音识别配置不能当对话默认（不进 load_providers/默认链，绑了必坏）
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"default_config {default!r} is an asr config"},
        )
    blocked = _guard_config_removal(
        {c["id"] for c in prov_mod.load_configs()}, new_ids, default
    )
    if isinstance(blocked, JSONResponse):
        return blocked
    saved = prov_mod.save_configs(normalized, default)
    await _apply_model_config_changes(stored)
    return {
        "ok": True,
        "configs": [
            {**c, "api_key": prov_mod.mask_secret(c.get("api_key"))} for c in saved
        ],
        "default_config": prov_mod.get_default_config(),
    }


@router.post("/api/model_configs/verify")
async def verify_model_config_endpoint(cfg: dict) -> dict:
    """Verify a config DRAFT; on success (decision Q4) the draft is saved
    immediately (upsert by id, ``verified_at`` stamped) and returned. On
    failure nothing is written and ``{ok: false, error}`` comes back with 200
    so the UI can show the yellow banner."""
    # SEC-01: the draft arrives with a masked key ("••••1234") when the user
    # edits an existing config. Resolve it ONCE and use the resolved draft for
    # both the probe and the save — the probe used to get the resolved copy
    # while the save wrote the raw one, so every Settings save of an existing
    # config persisted the mask over the real key (2026-10-24: 智谱 key became
    # "••••OrgQ"; the next request died inside httpx header encoding with a bare
    # UnicodeEncodeError). A mask that cannot be resolved (new/corrupted entry)
    # must be refused, never stored.
    draft = prov_mod.unmask_config_draft(cfg)
    if prov_mod.is_masked(draft.get("api_key")):
        return {
            "ok": False,
            "error": t(
                "The API key is still a mask placeholder — re-enter the real key",
                "API Key 仍是掩码占位值，请重新填写真实 Key",
            ),
        }
    from .. import voice_asr

    normalized = prov_mod.normalize_config(draft)
    if normalized["kind"] == "asr" and voice_asr.MODEL in normalized["models"]:
        if not voice_asr.endpoint(normalized):
            return {"ok": False, "error": t(
                "Enter a supported HTTPS workspace or gateway URL and an API key.",
                "请输入受支持的工作空间或网关 HTTPS 地址及 API Key。",
            )}
        result = await voice_asr.verify(normalized)
    else:
        result = await asyncio.to_thread(prov_mod.verify_config_draft, draft)
    if not result.get("ok"):
        return {
            "ok": False,
            "error": result.get("error"),
            "latency_ms": result.get("latency_ms"),
        }
    stored = {c["id"]: c for c in prov_mod.load_configs()}
    config_id = str(draft.get("id") or "").strip() or f"prov_{uuid.uuid4().hex[:8]}"
    previous = stored.get(config_id) or {}
    saved_cfg = prov_mod.normalize_config({"model_tags": previous.get("model_tags", {}), **draft})
    saved_cfg["verified_at"] = int(time.time())
    saved_cfg["last_error"] = None
    saved_cfg["id"] = config_id
    merged = {**stored, config_id: saved_cfg}
    try:
        saved = prov_mod.save_configs(list(merged.values()))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    saved_cfg = next(c for c in saved if c["id"] == config_id)
    await _apply_model_config_changes(stored)
    return {
        "ok": True,
        "config": {**saved_cfg, "api_key": prov_mod.mask_secret(saved_cfg.get("api_key"))},
        "latency_ms": result.get("latency_ms"),
    }


@router.post("/api/model_configs/list_models")
def list_model_config_models(cfg: dict) -> dict:
    """Fetch a config DRAFT's model catalogue from its provider (「从 API 拉取
    模型」). Nothing is saved — id not required. Sync so the network round-trip
    runs in the threadpool; HTTP 200 always, ok:false carries the error, same
    convention as the verify endpoint."""
    # SEC-01 同 verify 端点：草稿带着掩码 key 且反掩码失败（新配置/复制配置
    # 无存量 id 可解析）时必须拒绝——掩码 4 个 bullet 是非 ASCII，放进
    # x-api-key header 会在 httpx ASCII 编码处炸 UnicodeEncodeError 天书
    # （2026-10-29 真机：拉取模型列表报 'ascii' codec can't encode
    # characters in position 0-3，正是 MASK_PREFIX 的 4 个 •）。
    draft = prov_mod.unmask_config_draft(cfg)
    if prov_mod.is_masked(draft.get("api_key")):
        return {
            "ok": False,
            "error": t(
                "The API key is still a mask placeholder — re-enter the real key",
                "API Key 仍是掩码占位值，请重新填写真实 Key",
            ),
        }
    return prov_mod.list_models_draft(draft)


@router.delete("/api/model_configs/{config_id}")
async def delete_model_config(config_id: str):
    stored = {c["id"]: c for c in prov_mod.load_configs()}
    if config_id not in stored:
        return JSONResponse(
            status_code=404, content={"ok": False, "error": f"unknown config: {config_id}"}
        )
    refs = _config_references(config_id)
    if refs["agents"] or refs["is_default"]:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": f"Config {config_id} is still referenced and cannot be deleted",
                # same shape as the PUT removal guard: keyed by config id
                "references": {config_id: refs},
            },
        )
    remaining = [c for c in stored.values() if c["id"] != config_id]
    settings = _read_settings_file()
    was_default = (settings.get("default_config") or settings.get("default_provider")) == config_id
    new_default = None
    if was_default:
        # design §2.5: default moves to the first enabled config (or clears to
        # "" so the legacy default_provider fallback takes over); asr 卡不能
        # 当对话默认（load_providers/默认链都跳过它，绑了必坏）
        new_default = next(
            (c["id"] for c in remaining
             if c.get("enabled") and c.get("kind") != "asr"),
            "",
        )
    prov_mod.save_configs(remaining, new_default)
    await _apply_model_config_changes(stored)
    return {"ok": True, "default_config": prov_mod.get_default_config()}


# ---- providers (Settings → 模型 API, legacy read-only-compat view) ----


def _config_to_legacy(cfg: dict) -> dict:
    """Project a v2 config into the legacy slot-dict shape the pre-v2
    frontend reads: no ``id`` key (the dict key IS the id), and the custom
    slot keeps its historical ``model`` field name alongside ``default_model``."""
    out = {k: v for k, v in cfg.items() if k != "id"}
    out["default_model"] = cfg.get("default_model") or ""
    out.setdefault("model", out["default_model"])
    return out


@router.get("/api/providers")
def get_providers() -> dict:
    settings = _read_settings_file()
    configs = prov_mod.load_configs(settings)
    # kind="asr" 卡不进 legacy providers 视图——聊天侧（模型选择、agents
    # 下拉、会话绑定）全部经此读取；asr 卡按 id 供 ginno_runtime.asr 调用。
    legacy = {cfg["id"]: _config_to_legacy(cfg) for cfg in configs
              if cfg.get("kind") != "asr"}
    # SEC-01: same rule as GET /api/settings — keys never leave the process in
    # plaintext. PUT /api/providers restores masked ones from storage.
    for c in legacy.values():
        models = [*(c.get("models") or []), c["default_model"]]
        efforts = {
            model: levels for model in models
            if (levels := codex_efforts(c, model)) is not None
        }
        if efforts:
            c["model_thinking_efforts"] = efforts
        if c.get("api_key"):
            c["api_key"] = prov_mod.mask_secret(c["api_key"])
    return {
        "default_provider": prov_mod.get_default_config(settings),
        "providers": legacy,
    }


class PutProvidersRequest(BaseModel):
    providers: dict
    default_provider: str | None = None


def _refresh_session_metas(previous: dict[str, dict] | None = None) -> dict[str, dict]:
    """Re-resolve every session meta's provider/model against current config.

    Session metas persist provider/model from creation time; the topbar label
    and rebuilt graphs (``_ensure_session``) read them, so after a provider
    config change — or on startup, to heal metas frozen by older builds — they
    must be re-resolved. A session's own still-resolvable binding — the model
    picked at creation or switched via the chat model chip — is PRESERVED;
    only bindings that no longer resolve (provider disabled/deleted, model
    dropped from its list) fall back to the enabled agent provider, else the
    enabled global default.
    """
    from ..agents.registry import provider_is_deliberate
    from ..session_meta import _session_meta_update_all

    settings = _read_settings_file()
    providers = prov_mod.load_providers(settings)
    default_pid = prov_mod.get_default_provider(settings)
    refreshed = {}

    def enabled(pid: str | None) -> bool:
        return bool((providers.get(pid) or {}).get("enabled"))

    def allows(pid: str | None, model: str | None) -> bool:
        allowed = (providers.get(pid) or {}).get("models") or []
        return bool(model) and (not allowed or model in allowed)

    def refresh_binding(m: dict) -> dict:
        cur_pid = m.get("provider")
        cur_model = m.get("model")
        provider, model = cur_pid, cur_model
        if not enabled(cur_pid) or not allows(cur_pid, cur_model):
            ag = _agent_lookup(m.get("agent_id"))
            agent_pid = ag.provider if provider_is_deliberate(ag) else None
            provider = agent_pid if enabled(agent_pid) else default_pid
            agent_model = ag.model if ag and allows(provider, ag.model) else None
            model = agent_model or prov_mod.model_for_provider(providers, provider)

        saved_effort = m.get("thinking_effort")
        target_cfg = providers.get(provider)
        if previous is None and (provider, model) == (cur_pid, cur_model):
            eff = resolve_model_effort(target_cfg, model, saved_effort)
        else:
            source_cfg = (previous if previous is not None else providers).get(cur_pid)
            eff = inherit_model_effort(source_cfg, cur_model, target_cfg, model, saved_effort)
        effort = eff or "auto"
        binding_changed = (provider, model) != (cur_pid, cur_model)
        effort_changed = saved_effort is not None and effort != saved_effort
        if m.get("id") in _SESSIONS:
            refreshed[m["id"]] = {
                "provider": provider, "model": model, "thinking_effort": effort,
                "binding_changed": binding_changed, "effort_changed": effort_changed,
                "effort_reset": effort_changed and effort == "auto",
            }
        if binding_changed or effort_changed:
            return {"provider": provider, "model": model, "thinking_effort": effort}
        return {}

    projects_root = paths.home() / "projects"
    if projects_root.is_dir():
        for slug_dir in sorted(projects_root.iterdir()):
            if slug_dir.is_dir():
                _session_meta_update_all(slug_dir.name, refresh_binding)
    return refreshed


async def _apply_model_config_changes(previous: dict[str, dict]) -> None:
    from .sessions._base import build_model

    bindings = _refresh_session_metas(previous)
    events = []
    for sid, session in list(_SESSIONS.items()):
        binding = bindings.get(sid)
        if not binding or (sid not in shared._RUNNING_TURNS and not shared._SESSION_WS.get(sid)):
            _SESSIONS.pop(sid, None)
            continue
        provider, model = binding["provider"], binding["model"]
        effort = binding["thinking_effort"]
        try:
            built = build_model(
                provider, model, session_id=sid,
                thinking_effort=None if effort == "auto" else effort,
            )
        except ValueError as e:
            log.warning("session config refresh failed session=%s: %s", sid, e)
            _SESSIONS.pop(sid, None)
            continue
        # Keep the registered entry for the running turn and connected clients.
        # A model call already in flight keeps the client it acquired earlier.
        session.update(model=built, model_provider=provider, model_name=model, thinking_effort=effort)
        if binding["binding_changed"] or binding["effort_changed"]:
            events.append((sid, {
                "session_id": sid, "provider": provider, "model": model,
                "thinking_effort": effort, "effective": "next_step",
                "effort_reset": binding["effort_reset"],
            }))
    await _push_global_event("providers.changed", {})
    for sid, data in events:
        await shared._push_session_event(sid, "session.model_changed", data)


@router.put("/api/providers")
async def put_providers(req: PutProvidersRequest) -> dict:
    # Forward to the v2 store: the dict payload is converted to configs and
    # merged by id (configs not expressible in the legacy dict form survive);
    # the default is written to the NEW key only (default_config).
    # SEC-01: the payload round-trips what GET /api/providers handed out, i.e.
    # masked keys — resolve them against storage before saving, exactly like
    # PUT /api/model_configs does.
    stored = {c["id"]: c for c in prov_mod.load_configs()}
    providers: dict = {}
    for pid, cfg in (req.providers or {}).items():
        cfg = dict(cfg or {})
        cfg.setdefault("model_tags", (stored.get(pid) or {}).get("model_tags", {}))
        if prov_mod.is_masked(cfg.get("api_key")):
            prev_key = (stored.get(pid) or {}).get("api_key")
            if not prev_key:
                # 同 PUT /api/model_configs：掩码=未修改占位，置空忽略
                log.warning("providers_put_mask_placeholder_ignored id=%s", pid)
                prev_key = ""
            cfg["api_key"] = prev_key
        providers[pid] = cfg
    saved = prov_mod.save_providers(providers)
    default = req.default_provider
    if default:
        # 读-改-写走统一存储层（锁 + 原子写 + 损坏拒写），不再直接
        # write_text 整文件覆盖。
        def _set_default(doc: dict) -> dict:
            doc["default_config"] = default
            return doc

        try:
            settings_store.update(_set_default)
        except settings_store.SettingsCorruptError as e:
            return env_settings_api.corrupt_response(e)
        except settings_store.SettingsWriteError as e:
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": "write_failed", "detail": str(e)},
            )
    else:
        default = prov_mod.get_default_provider()
    await _apply_model_config_changes(stored)
    # Same SEC-01 outbound rule as GET: the echo carries no plaintext keys.
    echo = {pid: {**cfg, "api_key": prov_mod.mask_secret(cfg.get("api_key"))}
            for pid, cfg in saved.items()}
    return {"ok": True, "providers": echo, "default_provider": default}


@router.post("/api/providers/{provider_id}/verify")
async def verify_provider(provider_id: str) -> dict:
    return await asyncio.to_thread(prov_mod.verify, provider_id)


@router.post("/api/providers/{provider_id}/search_probe")
def provider_search_probe(provider_id: str) -> dict:
    """User-triggered (the 测试联网 button) probe of the model's built-in web
    search. Sync so the network round-trip runs in the threadpool, not on the
    event loop."""
    from .. import providers as _prov

    return _prov.search_probe(provider_id)


# ---- agents ----


@router.get("/api/agents")
async def list_agents_endpoint() -> list[dict]:
    return [a.to_dict() for a in agents_reg.list_agents()]


@router.get("/api/agents/{agent_id}")
async def get_agent_endpoint(agent_id: str) -> dict:
    """Read one agent. Unknown id → 404 JSON (the GET used to fall through to
    the SPA catch-all and answer 200 HTML)."""
    agent = agents_reg.get_agent(agent_id)
    if agent is None:
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "unknown agent"}
        )
    return agent.to_dict()


@router.get("/api/subagent_types")
async def list_subagent_types_endpoint() -> list[dict]:
    """Agents 页 Sub-agent models 区块的数据源（model-assignment-design.md
    §2.3）：[{name, description, model, tools_allow, body}]——model 为类型
    frontmatter 的默认覆盖原文（provider/model 或裸名），供 UI 显示继承链
    落点；tools_allow/body 供 Sub-agent types 编辑区回填。"""
    from ..subagent_types import load_subagent_types

    return [
        {
            "name": t.name,
            "description": t.description,
            "model": t.model,
            "tools_allow": t.tools_allow,
            "body": t.body,
        }
        for t in sorted(load_subagent_types().values(), key=lambda t: t.name)
    ]


@router.put("/api/subagent_types/{name}")
async def update_subagent_type_endpoint(name: str, data: dict) -> dict:
    """Settings → Agents → Sub-agent types 的保存端点：原地改写类型 md 文件
    （description / tools_allow / model / persona body；name 是文件标识不可
    改）。tools_allow=[] 删除该键 = 不收紧（子代继承全量并集工具集，含
    MCP）。失败走 {ok:false, error} 200——与 agents 端点同一约定。"""
    from ..subagent_types import update_subagent_type

    if "tools_allow" in data and not isinstance(data.get("tools_allow"), list):
        return {"ok": False, "error": "tools_allow must be a list of patterns"}
    fields = {
        k: data.get(k)
        for k in ("description", "tools_allow", "model", "body")
        if k in data
    }
    t = update_subagent_type(name, **fields)
    if t is None:
        return {"ok": False, "error": f"unknown subagent type: {name}"}
    return {
        "ok": True,
        "type": {
            "name": t.name,
            "description": t.description,
            "model": t.model,
            "tools_allow": t.tools_allow,
            "body": t.body,
        },
    }


@router.post("/api/agents")
async def create_agent_endpoint(data: dict) -> dict:
    try:
        agent = agents_reg.create_agent(data).to_dict()
    except AgentModelBindingError as e:
        # decision Q3: hard constraint violations are a real 400
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    await _push_global_event("agents.changed", {})
    return {"ok": True, "agent": agent}


@router.put("/api/agents/{agent_id}")
async def update_agent_endpoint(agent_id: str, data: dict) -> dict:
    try:
        agent = agents_reg.update_agent(agent_id, data).to_dict()
    except AgentModelBindingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    await _push_global_event("agents.changed", {})
    return {"ok": True, "agent": agent}


@router.delete("/api/agents/{agent_id}")
async def delete_agent_endpoint(agent_id: str) -> dict:
    ok = agents_reg.delete_agent(agent_id)
    if ok:
        await _push_global_event("agents.changed", {})
    return {"ok": ok}


@router.post("/api/agents/reorder")
async def reorder_agents_endpoint(data: dict) -> dict:
    """持久化 agents.order（agent 排序设计 D3）。列表第一位 = 新会话默认
    agent（D1 耦合）。读-改-写 settings.json 而非复用 PUT /settings ——
    那是整文件覆盖，会和其它设置页的并发保存互踩。"""
    order = data.get("order")
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        return {"ok": False, "error": "order must be a list of agent ids"}
    # 必须恰好覆盖当前 agent 集合（无缺、无重、无幽灵）——防并发增删
    # 期间的漂移顺序写进 settings
    current = {a.id for a in agents_reg.list_agents()}
    if len(order) != len(set(order)) or set(order) != current:
        return {"ok": False, "error": "order must list every agent exactly once"}
    agents_reg.set_agent_order(order)
    await _push_global_event("agents.changed", {})
    return {"ok": True}
