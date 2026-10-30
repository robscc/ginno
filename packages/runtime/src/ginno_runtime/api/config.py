"""Settings/config endpoints: general settings, model providers, agents,
MCP server config, and skills management."""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ginno_runtime import jsonx as json

from .. import agents as agents_reg
from .. import paths
from .. import providers as prov_mod
from .. import server_shared as shared
from ..agents.registry import AgentModelBindingError
from ..lang import t
from ..mcp.registry import MCPRegistry
from ..server_shared import _SESSIONS, _push_global_event
from ..session_meta import _session_meta_list
from ..skills.loader import SkillLoader

router = APIRouter()

log = logging.getLogger(__name__)


# ---- skills ----


@router.get("/api/skills")
async def list_skills(project_slug: str | None = None) -> list[dict]:
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
async def get_skill_body(name: str, project_slug: str | None = None) -> dict:
    s = SkillLoader(project_slug=project_slug).get(name)
    return {"ok": bool(s), "body": s.body if s else ""}


@router.post("/api/skills")
async def create_skill_endpoint(data: dict) -> dict:
    name = (data.get("name") or "").strip()
    body = data.get("body") or ""
    if not name:
        return {"ok": False, "error": "name required"}
    d = paths.global_skills_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    await _push_global_event("skills.changed", {})
    return {"ok": True}


@router.delete("/api/skills/{name}")
async def delete_skill_endpoint(name: str) -> dict:
    import shutil

    d = paths.global_skills_dir() / name
    if d.exists():
        shutil.rmtree(d)
        await _push_global_event("skills.changed", {})
        return {"ok": True}
    s = SkillLoader().get(name)
    if s and s.builtin:
        return {"ok": False, "error": "builtin skill cannot be deleted"}
    return {"ok": False}


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

    result = import_skills_from_dir(
        (data or {}).get("path", ""),
        overwrite=bool((data or {}).get("overwrite", False)),
    )
    if result.get("ok") and result.get("imported"):
        await _push_global_event("skills.changed", {})
    return result


# ---- mcp ----


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


@router.get("/api/mcp/config")
async def get_mcp_config_endpoint() -> dict:
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


@router.put("/api/mcp")
async def put_mcp_endpoint(data: dict) -> dict:
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
async def get_settings() -> dict:
    p = paths.settings_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {}
    # SEC-01: never hand provider keys back in plaintext. PUT restores them.
    return prov_mod.mask_settings(raw)


@router.put("/api/settings")
async def put_settings(data: dict) -> dict:
    prev_use_proxy = prov_mod.use_system_proxy()  # reads the still-on-disk value
    # Undo the GET masking before writing: callers round-trip the whole
    # document (e.g. the proxy toggle), so a masked key must resolve back to
    # the stored secret instead of being persisted as "••••1234".
    try:
        previous = json.loads(paths.settings_path().read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        previous = {}
    data = prov_mod.unmask_settings(data, previous)
    # 2026-10-29 review 定稿：残留掩码（草稿引用了存储里不存在的条目）=
    # 「未修改」占位，置空忽略而非报错；绝不把 •••• 写进 settings.json
    # ——掩码落盘后，聊天/分类器等真实流量会带着它出网，在 httpx ASCII
    # 编码处炸 UnicodeEncodeError（2026-10-24 智谱 key 事故同型）。
    cleared = prov_mod.clear_masked_leaves(data)
    if cleared:
        log.warning("settings_put_mask_placeholder_ignored fields=%s",
                     ",".join(cleared[:10]))
    paths.settings_path().write_text(json.dumps(data, indent=2, ensure_ascii=False))
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
    return (
        json.loads(paths.settings_path().read_text() or "{}")
        if paths.settings_path().exists()
        else {}
    )


@router.get("/api/model_configs")
async def get_model_configs() -> dict:
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
        if isinstance(c, dict) and prov_mod.is_masked(c.get("api_key")):
            c = dict(c)
            prev = stored.get(c.get("id")) or {}
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
    # Same eviction semantics as the legacy PUT /api/providers: drop cached
    # session graphs and re-resolve persisted metas against the new config.
    _SESSIONS.clear()
    _refresh_session_metas()
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
    result = prov_mod.verify_config_draft(draft)
    if not result.get("ok"):
        return {
            "ok": False,
            "error": result.get("error"),
            "latency_ms": result.get("latency_ms"),
        }
    saved_cfg = prov_mod.normalize_config(draft)
    saved_cfg["verified_at"] = int(time.time())
    saved_cfg["last_error"] = None
    if not saved_cfg.get("id"):
        saved_cfg["id"] = f"prov_{uuid.uuid4().hex[:8]}"
    merged = {c["id"]: c for c in prov_mod.load_configs()}
    merged[saved_cfg["id"]] = saved_cfg
    prov_mod.save_configs(list(merged.values()))
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
    _SESSIONS.clear()
    _refresh_session_metas()
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
async def get_providers() -> dict:
    settings = _read_settings_file()
    configs = prov_mod.load_configs(settings)
    # kind="asr" 卡不进 legacy providers 视图——聊天侧（模型选择、agents
    # 下拉、会话绑定）全部经此读取；asr 卡按 id 供 ginno_runtime.asr 调用。
    legacy = {cfg["id"]: _config_to_legacy(cfg) for cfg in configs
              if cfg.get("kind") != "asr"}
    # SEC-01: same rule as GET /api/settings — keys never leave the process in
    # plaintext. PUT /api/providers restores masked ones from storage.
    for c in legacy.values():
        if c.get("api_key"):
            c["api_key"] = prov_mod.mask_secret(c["api_key"])
    return {
        "default_provider": prov_mod.get_default_config(settings),
        "providers": legacy,
    }


class PutProvidersRequest(BaseModel):
    providers: dict
    default_provider: str | None = None


def _refresh_session_metas() -> None:
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
    providers = prov_mod.load_providers()

    def _enabled(pid: str | None) -> bool:
        return bool(pid) and bool((providers.get(pid) or {}).get("enabled"))

    projects_root = paths.home() / "projects"
    if not projects_root.is_dir():
        return
    for slug_dir in sorted(projects_root.iterdir()):
        if not slug_dir.is_dir():
            continue
        slug = slug_dir.name
        metas = _session_meta_list(slug)
        if not metas:
            continue
        changed = False
        for m in metas:
            ag = _agent_lookup(m.get("agent_id"))
            # 会话自身的绑定优先保留:新建会话时的选择、模型 chip 的切换都是
            # 用户显式动作,meta 里的 provider/model 即意图,不能被重算打回
            # (否则「选好的模型重启后悄悄变回 agent 绑定」)。仅当绑定已不可
            # 解析(provider 被禁/删,或 model 不在其 models 列表;列表为空
            # 视为不限,兼容手填 model 的用法)才继续走 agent/默认重算。
            cur_pid = m.get("provider")
            cur_model = m.get("model")
            if _enabled(cur_pid):
                allowed = (providers.get(cur_pid) or {}).get("models") or []
                if cur_model and (not allowed or cur_model in allowed):
                    continue
            # Same precedence as _resolve_provider_model (2026-10-02 fix):
            # the agent's provider counts only as a DELIBERATE choice — one
            # differing from the global default. The seed placeholder
            # (provider == default) must not shadow the user's 默认模型提供商.
            default_pid = prov_mod.get_default_provider()
            from ..agents.registry import provider_is_deliberate

            provider = next(
                (
                    c
                    for c in [
                        (ag.provider if provider_is_deliberate(ag) else None),
                        default_pid,
                    ]
                    if _enabled(c)
                ),
                None,
            ) or prov_mod.get_default_provider()
            model = (ag.model if ag and ag.model else None) or prov_mod.model_for_provider(
                providers, provider
            )
            if m.get("provider") != provider or m.get("model") != model:
                m["provider"] = provider
                m["model"] = model
                changed = True
        if changed:
            # 整表替换必须走 session_meta 的锁+原子写(直接 write_text 会绕开
            # 2026-10-05 事故后加上的串行约束)。
            from ..session_meta import _session_meta_rewrite_all

            _session_meta_rewrite_all(slug, metas)


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
        settings = _read_settings_file()
        settings["default_config"] = default
        paths.settings_path().write_text(
            json.dumps(settings, indent=2, ensure_ascii=False)
        )
    else:
        default = prov_mod.get_default_provider()
    # Evict all cached session graphs so the next WS connection rebuilds
    # with the freshly saved model/provider — otherwise existing sessions
    # keep using the LLM client that was frozen at session creation.
    _SESSIONS.clear()
    # Session metas persist provider/model from creation time; the topbar and
    # rebuilt graphs read them, so re-resolve them against the just-saved
    # config.
    _refresh_session_metas()
    # Same SEC-01 outbound rule as GET: the echo carries no plaintext keys.
    echo = {pid: {**cfg, "api_key": prov_mod.mask_secret(cfg.get("api_key"))}
            for pid, cfg in saved.items()}
    return {"ok": True, "providers": echo, "default_provider": default}


@router.post("/api/providers/{provider_id}/verify")
async def verify_provider(provider_id: str) -> dict:
    return prov_mod.verify(provider_id)


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
