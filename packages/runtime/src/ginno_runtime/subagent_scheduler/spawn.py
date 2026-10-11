"""Spawn path (contract 4/5, design §5.1/§5.3): depth + mod + concurrency
gates, child session creation through api.sessions, brief construction, and
the launch of the child's first managed turn."""

from __future__ import annotations

import sys

_NS = sys.modules[__package__]  # package namespace: all former module globals


def _agent_type_model_override(agent_id: str | None, type_name: str) -> tuple[str, str] | None:
    """Agents 页 per-agent per-type 模型覆盖（model-assignment-design.md §2.2，
    解析链最高层）。绑定失效（provider 禁用/删除、模型脱离 models[]、provider
    无默认模型）时降级：警告 + 返回 None，落回类型 frontmatter / 父会话
    模型——比一路跌到全局默认更接近用户意图。"""
    if not agent_id or not type_name:
        return None
    from .. import providers as prov_mod
    from ..agents.registry import get_agent

    agent = get_agent(agent_id)
    binding = (agent.subagent_models if agent is not None else None) or {}
    binding = binding.get(type_name)
    if not isinstance(binding, dict):
        return None
    provider = str(binding.get("provider") or "").strip()
    if not provider:
        return None
    providers = prov_mod.load_providers()
    if not (providers.get(provider) or {}).get("enabled"):
        _NS._log.warning(
            "agent_type_model_override_dropped agent=%s type=%s provider=%s reason=disabled",
            agent_id, type_name, provider,
        )
        return None
    # 显式 model 查成员（同 validate_model_binding 的硬约束）；空串 = 该
    # provider 的默认模型，解析自配置本身，不再反向查成员。
    explicit = str(binding.get("model") or "").strip()
    listed = (prov_mod.get_config(provider) or {}).get("models") or []
    if explicit and listed and explicit not in listed:
        _NS._log.warning(
            "agent_type_model_override_dropped agent=%s type=%s model=%s reason=not-in-models",
            agent_id, type_name, explicit,
        )
        return None
    model = explicit or prov_mod.model_for_provider(providers, provider)
    if not model:
        _NS._log.warning(
            "agent_type_model_override_dropped agent=%s type=%s provider=%s reason=no-default-model",
            agent_id, type_name, provider,
        )
        return None
    return provider, model


async def create_subagent(
    parent_session_id: str,
    goal: str,
    constraints: str = "",
    acceptance: str = "",
    origin: str = "agent",
    agent_type: str = "",
    fork: bool = False,
) -> dict:
    """Create + launch one child session. Never raises; ``ok: False`` results
    carry an ``[error]``-style message the tool returns verbatim.

    P3 contract 1/2: ``agent_type`` routes through the type registry (persona
    body prepended to the brief, toolset tightened by the intersected allow
    list, optional model override); ``fork`` seeds the child's initial history
    with a copy of the parent's checkpoint messages (ALL ids freshly minted)
    plus the standalone brief at the top. A fork child itself refuses
    ``fork=True``; fork does NOT change the depth rules."""
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "error": _NS.t("goal must not be empty", "goal 不能为空")}
    from ..api.sessions import _ensure_session  # lazy: cycle

    parent = _NS._SESSIONS.get(parent_session_id) or _ensure_session(parent_session_id)
    if parent is None:
        return {
            "ok": False,
            "error": _NS.t(
                f"unknown parent session {parent_session_id}",
                f"未知父会话 {parent_session_id}",
            ),
        }
    slug = parent["project_slug"]
    found = _NS._find_meta(parent_session_id)
    parent_meta = found[0] if found else {}

    depth = _NS.subagent_depth_of(parent_meta or parent) + 1
    if depth > _NS.SUBAGENT_MAX_DEPTH:
        return {
            "ok": False,
            "error": _NS.t(
                f"[error] Maximum nesting depth ({_NS.SUBAGENT_MAX_DEPTH + 1} "
                "layers) reached; no further delegation. Do the work yourself.",
                f"[error] 已达最大嵌套深度（{_NS.SUBAGENT_MAX_DEPTH + 1} 层），"
                "不能再委派子任务。请自己完成该工作。",
            ),
        }

    # Type registry (P3 contract 1). Unknown → [error] + the available list,
    # so the model can correct itself in the next call.
    st = None
    if (agent_type or "").strip():
        from ..subagent_types import describe_types, get_subagent_type

        st = get_subagent_type(agent_type)
        if st is None:
            _types = describe_types()
            avail = _NS.t(
                ", ".join(x["name"] for x in _types) or "(registry is empty)",
                "、".join(x["name"] for x in _types) or "（注册表为空）",
            )
            return {
                "ok": False,
                "error": _NS.t(
                    f"[error] Unknown subagent type: {agent_type.strip()}. "
                    f"Available types: {avail}",
                    f"[error] 未知 subagent 类型：{agent_type.strip()}。"
                    f"可用类型：{avail}",
                ),
            }
    # Fork lineage (P3 contract 2): a fork child cannot fork again. P1/P2
    # metas have no ``mode`` field and read as "standard".
    parent_mode = str(
        ((parent_meta.get("subagent") or {}).get("mode")) or "standard"
    )
    if fork and parent_mode == "fork":
        return {
            "ok": False,
            "error": _NS.t(
                "[error] A fork child cannot fork again. Use a standard spawn "
                "(no inherited context).",
                "[error] fork 子代不能再 fork。请使用标准 spawn（不继承上下文）。",
            ),
        }

    # Mods tap (claude-code-mods-design.md §5.3 agent.spawn): the chain may
    # answer {deny} to refuse the spawn outright. Placed after the cheap
    # validations (an invalid spawn should not burn a mod hook) and before any
    # state changes. P2 does not honor a model rewrite — observe or deny only.
    from ..mods.events import dispatch_agent_spawn

    verdict = await dispatch_agent_spawn(
        parent_session_id,
        st.name if st is not None else (agent_type or "").strip(),
        goal,
        origin=origin,
        depth=depth,
    )
    if verdict:
        reason = str(verdict.get("deny") or "denied by mod")
        mod_name = str(verdict.get("mod") or "")
        by = f" {mod_name}" if mod_name else ""
        await _NS._push_session_event(
            parent_session_id,
            "notice",
            {
                "message": (
                    f"Subagent spawn denied by mod {mod_name}: {reason}"
                    if mod_name
                    else f"Subagent spawn denied by a mod: {reason}"
                ),
                "i18n_key": "chat.mods.agentSpawnDenied",
                "params": {"mod": mod_name, "reason": reason},
            },
        )
        _NS._log.info(
            "subagent_spawn_denied_by_mod parent=%s mod=%s reason=%r goal=%r",
            parent_session_id, mod_name, reason, goal[:80],
        )
        return {
            "ok": False,
            "error": _NS.t(
                f"[error] Subagent spawn denied by mod{by}: {reason}",
                f"[error] 子代理启动被 mod{by}拒绝：{reason}",
            ),
        }

    cap = _NS.max_concurrent()
    async with _NS._SPAWN_LOCK:
        running = _NS._running_subagent_metas()
        if len(running) >= cap:
            listing = "\n".join(
                _NS.t(
                    f"- {m['id']} {((m.get('subagent') or {}).get('goal') or '')[:40]}"
                    f" ({_NS._subagent_status(m)})",
                    f"- {m['id']} {((m.get('subagent') or {}).get('goal') or '')[:40]}"
                    f"（{_NS._subagent_status(m)}）",
                )
                for m in running[:cap]
            )
            return {
                "ok": False,
                "limit": True,
                "error": _NS.t(
                    f"[error] Concurrent subagent cap reached ({cap}); do not "
                    f"retry immediately. Currently running:\n{listing}",
                    f"[error] 并发 subagent 已达上限（{cap}），"
                    f"勿立即重试。当前在跑清单：\n{listing}",
                ),
            }

        from ..api.sessions import CreateSessionRequest, create_session  # lazy: cycle

        provider, model_name = parent.get("model_provider"), parent.get("model_name")
        if st is not None:
            from ..subagent_types import resolve_type_model

            # 解析链（model-assignment-design.md §2.2）：类型 frontmatter 覆盖
            # 父会话模型；Agents 页 per-agent per-type 覆盖再压一层（最高
            # 优先，失效时在 helper 内降级）。
            provider, model_name = resolve_type_model(
                st, (provider, model_name)
            )
            override = _agent_type_model_override(parent.get("agent_id"), st.name)
            if override is not None:
                provider, model_name = override
        title = goal.replace("\n", " ")[:40]
        req = CreateSessionRequest(
            project_slug=slug,
            workspace=str(parent.get("workspace") or ""),
            agent_id=parent.get("agent_id"),
            title=title,
            type="subagent",
            # Inherit the parent's model config + mounts; the workspace itself
            # stays per-session (create_session supersedes it by design). A
            # matched type may override the model (frontmatter), and the Agents
            # 页 per-agent override outranks it; the type also tightens tools.
            provider=provider,
            model=model_name,
            context_folders=list(parent_meta.get("context_folders") or []),
            primary_folder=parent_meta.get("primary_folder") or None,
            parent_session_id=parent_session_id,
            restrict_tools=list(st.tools_allow) if st is not None else [],
            subagent={
                "goal": goal,
                "constraints": constraints or "",
                "acceptance": acceptance or "",
                "origin": origin,
                "status": "running",
                "result_summary": "",
                # P3 contract 2: "standard" | "fork"; absent (P1/P2 legacy
                # metas) reads as standard everywhere.
                "mode": "fork" if fork else "standard",
                "agent_type": st.name if st is not None else "",
            },
        )
        resp = await create_session(req)
        if not resp.get("ok"):
            return {
                "ok": False,
                "error": str(
                    resp.get("error")
                    or _NS.t("child session creation failed", "子会话创建失败")
                ),
            }
        child_id = resp["id"]

    # Pre-arm the cooperative stop event BEFORE the turn task exists (the same
    # protocol the WS invoke loop uses): a user kill in the spawn window must
    # reach the turn. Consumed + popped by _run_managed_turn.
    stop_evt = _NS._TURN_STOP.setdefault(child_id, _NS.asyncio.Event())

    brief = _NS.build_subagent_brief(
        goal, constraints, acceptance, depth,
        fork=fork,
        persona_body=st.body if st is not None else "",
    )
    if fork:
        # Seed the fork child's checkpoint: brief at the TOP + the parent's
        # current messages (fresh ids). Failure degrades to a standard spawn
        # (the brief still runs as the first turn) — a fork must never fail
        # the whole spawn.
        try:
            await _NS._seed_fork_history(parent_session_id, child_id, slug, brief)
            first_text = _NS._fork_start_text()
        except Exception:
            _NS._log.exception(
                "subagent_fork_seed_failed parent=%s child=%s",
                parent_session_id, child_id,
            )
            first_text = brief
    else:
        first_text = brief
    await _NS._push_both(
        parent_session_id,
        child_id,
        "subagent.spawned",
        {
            "session_id": child_id,
            "parent_session_id": parent_session_id,
            "goal": goal,
            "constraints": constraints or "",
            "acceptance": acceptance or "",
            "depth": depth,
            "origin": origin,
            "title": title,
            "mode": "fork" if fork else "standard",
            "agent_type": st.name if st is not None else "",
        },
    )
    _NS._log.info(
        "subagent_spawn parent=%s child=%s depth=%d origin=%s mode=%s type=%s goal=%r",
        parent_session_id, child_id, depth, origin,
        "fork" if fork else "standard", st.name if st is not None else "",
        goal[:80],
    )
    _NS.spawn_bg(_NS._run_managed_turn(child_id, first_text, None, stop_evt))
    await _NS._maybe_warn_concurrency(parent_session_id)
    return {"ok": True, "session_id": child_id, "title": title, "depth": depth}
