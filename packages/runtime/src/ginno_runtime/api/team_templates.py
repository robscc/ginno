"""内置团队模板（AgentTeam presets）。

Prompt → Team（team_generate.py）覆盖「一句话建队」；本模块补上
「选模板建队」：无需 LLM、零成本、结果确定，适合新手或重复场景。
模板是抽象的 TeamSpec（成员带 ``hints`` 关键词），应用时才解析为
当前注册表里真实存在的 agent id——同一模板在不同安装上落点不同。

参考：dsh-cabinet-mode（DeepSeek Harness 内阁模式 bundle）的分工
与回报契约；prompt 层约束，非运行时强制。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import agents as agents_reg
from . import team_generate as team_gen_api

router = APIRouter()

# 回报契约（反糊弄三件套），借鉴 cabinet-mode「御下术」红线，
# 各模板的 leader instructions 共用。
_REPORT_CONTRACT = (
    "回报纪律：报完成必须附路径、行号、命令输出与「未验证清单」，"
    "没跑说没跑，不得删弱校验凑绿；遇阻回报「已做、卡哪、需要什么」三件；"
    "同一失败最多重试 2 次，到限如实报「失败/未验证」。"
)


def _t(
    tid: str,
    name: str,
    description: str,
    charter: str,
    members: list[dict],
    first_task: str = "",
) -> dict:
    return {
        "id": tid,
        "name": name,
        "description": description,
        "charter_md": charter,
        "first_task": first_task,
        "members": members,
    }


def _m(
    display_name: str,
    role: str,
    instructions: str,
    hints: list[str],
) -> dict:
    return {
        "display_name": display_name,
        "role": role,
        "instructions": instructions,
        "hints": hints,
    }


TEAM_TEMPLATES: list[dict] = [
    _t(
        "cabinet",
        "内阁模式",
        "首辅统摄、阁臣分曹：调研 → 执行 → 核验 → 验收四权分立（借鉴 dsh-cabinet-mode）",
        "## 内阁章程\n\n- 首辅统摄全局、拆解派活；阁臣各司一职，只向首辅回报。\n"
        "- 默认调研先行，未经核验不改主线代码；结论锚定路径、行号或命令输出。\n"
        "- " + _REPORT_CONTRACT + "\n",
        [
            _m(
                "首辅",
                "leader",
                "统摄全局：拆解任务、按职派活、约定回报节点与产物范围；"
                "对成员施压只落在可核验的交付物与判据上，对用户只有敬。"
                + _REPORT_CONTRACT,
                ["lead", "leader", "shoufu", "main", "default"],
            ),
            _m(
                "翰林院编修",
                "member",
                "主调研考据：先读原文与既有记录，结论锚定路径、行号或命令输出，"
                "未实测标「未验证」；只调研不改码。",
                ["explore", "research", "researcher", "hanlin"],
            ),
            _m(
                "工部尚书",
                "member",
                "主执行与产出：先复述任务与写入范围，只在授权内改文件；"
                "产出附路径、行号、命令输出与退出码；保留失败证据。",
                ["impl", "implementer", "dev", "coder", "works"],
            ),
            _m(
                "大理寺卿",
                "member",
                "主独立核验：不信自述、自己跑测试与命令取证；"
                "逐项写「通过/不通过/未验证」，失败证据原样保留。",
                ["review", "reviewer", "test", "qa", "dali"],
            ),
        ],
        "请全体阁臣先自我介绍分工，然后由首辅拆解当前任务并派活（先调研后执行）。",
    ),
    _t(
        "software-delivery",
        "软件交付小队",
        "实现 ×2 并行 + 评审把关，适合可切分的编码任务",
        "## 交付章程\n\n- 一分支一主题；实现者只改约定范围内的文件，互不越界。\n"
        "- 提交前最小验证集全绿才交付；" + _REPORT_CONTRACT + "\n",
        [
            _m(
                "技术负责人",
                "leader",
                "拆分任务、划定各实现者的文件边界与接口约定；合并前亲自过验收清单。",
                ["lead", "leader", "main", "default"],
            ),
            _m(
                "实现 A",
                "member",
                "在指定文件范围内独立完成第一块实现；不碰约定范围外的文件。",
                ["impl", "implementer", "dev", "coder"],
            ),
            _m(
                "实现 B",
                "member",
                "在指定文件范围内独立完成第二块实现；与实现 A 的接口对齐以任务书为准。",
                ["impl2", "implementer", "dev", "coder"],
            ),
            _m(
                "评审",
                "member",
                "只读审查两块实现，给出可核对的问题清单（按严重度分级），可跑测试取证不改码。",
                ["review", "reviewer", "test"],
            ),
        ],
    ),
    _t(
        "research",
        "调研小队",
        "检索 ×2 交叉验证 + 汇总成稿，适合技术选型与资料研究",
        "## 调研章程\n\n- 结论必须带来源引用；两路检索结果冲突时如实并列，不擅自裁决。\n"
        "- 汇总稿先给结论表，再给分主题论据。\n",
        [
            _m(
                "主笔",
                "leader",
                "定调研问题清单、分派检索方向；汇总裁决结构，冲突结论如实标注。",
                ["lead", "leader", "main", "default"],
            ),
            _m(
                "检索甲",
                "member",
                "按问题清单做第一路检索，每条结论附来源链接与访问日期。",
                ["research", "researcher", "explore"],
            ),
            _m(
                "检索乙",
                "member",
                "独立做第二路交叉验证检索，重点核对甲路结论的反例与时效性。",
                ["research2", "researcher", "explore"],
            ),
        ],
    ),
    _t(
        "acceptance",
        "验收小队",
        "负责人对表 + 双线独立核验，适合上线前逐项验收",
        "## 验收章程\n\n- 按验收清单逐项核对，每项结论只有三种：通过 / 不通过 / 未验证。\n"
        "- 不通过必须附复现步骤与证据；未验证不算通过。\n",
        [
            _m(
                "验收组长",
                "leader",
                "产出验收清单（对齐用户原话），分派双线核验，汇总签署「可交/不可交」结论。",
                ["lead", "leader", "main", "default"],
            ),
            _m(
                "核验一",
                "member",
                "独立按清单逐项跑测试与命令取证，逐项写结论与证据。",
                ["review", "reviewer", "test", "qa"],
            ),
            _m(
                "核验二",
                "member",
                "换一条路径独立复核，重点抽查核验一的「通过」项，不许信自述。",
                ["review2", "reviewer", "test"],
            ),
        ],
    ),
]

_MAX_MEMBERS = 6


def _resolve_agents() -> list:
    return agents_reg.list_agents()


def _pick(agents: list, hints: list[str], used: set[str], fallback_idx: int) -> str:
    """hints 命中（id/name 子串，不区分大小写）优先，其次取未被占用的
    agent（轮转起点 = fallback_idx），最后允许复用（同一 agent 多实例）。"""
    lowered = [(str(a.id).lower(), str(a.name).lower(), str(a.id)) for a in agents]
    for hint in hints:
        h = hint.lower()
        for aid_l, name_l, aid in lowered:
            if h in aid_l or h in name_l:
                return aid
    free = [aid for _, _, aid in lowered if aid not in used]
    if free:
        return free[fallback_idx % len(free)]
    return lowered[fallback_idx % len(lowered)][2]


def resolve_template(template_id: str) -> dict:
    """模板 → 可建队的 TeamSpec（member 带真实 agent_id）。"""
    tpl = next((t for t in TEAM_TEMPLATES if t["id"] == template_id), None)
    if tpl is None:
        raise HTTPException(
            status_code=404,
            detail={"ok": False, "error": f"unknown template: {template_id}"},
        )
    agents = _resolve_agents()
    if not agents:
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "error": "no agents registered"},
        )
    used: set[str] = set()
    members: list[dict] = []
    for i, m in enumerate(tpl["members"][:_MAX_MEMBERS]):
        agent_id = _pick(agents, m.get("hints") or [], used, i)
        used.add(agent_id)
        members.append(
            {
                "agent_id": agent_id,
                "display_name": m["display_name"],
                "role": m["role"],
                "instructions": m["instructions"],
            }
        )
    spec = {
        "name": tpl["name"],
        "charter_md": tpl["charter_md"],
        "members": members,
        "first_task": tpl["first_task"],
    }
    return {"ok": True, "template": tpl, "spec": team_gen_api._normalize_spec(spec)}


class FromTemplateRequest(BaseModel):
    template_id: str
    project_slug: str = "default"
    workspace: str = ""
    agent_id: str | None = None
    first_task: str = ""


@router.get("/api/teams/templates")
async def list_templates() -> dict:
    return {
        "ok": True,
        "templates": [
            {
                "id": t["id"],
                "name": t["name"],
                "description": t["description"],
                "member_names": [m["display_name"] for m in t["members"]],
            }
            for t in TEAM_TEMPLATES
        ],
    }


@router.get("/api/teams/templates/{template_id}")
async def preview_template(template_id: str) -> dict:
    """预览：模板解析为具体 TeamSpec（不落盘）。"""
    return resolve_template(template_id)


@router.post("/api/teams/from_template")
async def team_from_template(req: FromTemplateRequest) -> dict:
    """按模板一键建队（复用 from_spec 的两步式下半场）。"""
    resolved = resolve_template(req.template_id)
    spec = resolved["spec"]
    first_task = req.first_task if req.first_task is not None else spec["first_task"]
    return await team_gen_api.team_from_spec(
        team_gen_api.FromSpecRequest(
            name=spec["name"],
            charter_md=spec["charter_md"],
            project_slug=req.project_slug,
            members=spec["members"],
            first_task=first_task,
            workspace=req.workspace,
            agent_id=req.agent_id,
        )
    )
