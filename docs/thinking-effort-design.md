# 会话级思考强度(thinking effort)— 产品设计

> 2026-11-02 定稿。四个关键决策已拍板(§10);本文是 P0 实现的契约基线。

## §0 背景与现状(链路事实)

- thinking 控制现状只有 **provider 配置级 `enable_thinking` 布尔**(`providers.py:148`),
  仅 openai-compat 分支生效(`models.py:607`,Qwen3 系方言)。anthropic 协议的
  extended thinking、openai-responses 的 `reasoning_effort` 完全没接。
- 会话级模型配置已有成熟通道,**本特性全部复用、零新机制**:
  - 会话记录 = 每项目 `sessions/_index.json` 里的 dict(`session_meta.py`);前端
    `SessionMeta`(`types.ts:170`)只有 `provider`/`model`,无任何 thinking 字段。
  - 换模型热切:PATCH `/api/sessions/{id}`(`sessions.py:893`)→ `build_model` 重建
    → 原地换 `_SESSIONS[sid]["model"]` → graph 每次 LLM 调用前经
    `make_session_model_getter`(`server_shared.py:553`)取值 → 运行中 turn 下一
    superstep 生效;同时广播 `session.model_changed {provider, model,
    effective:"next_step"}`。
  - 前端入口:composer 的 `ModelChipMenu`(`chat/stream/ModelChipMenu.tsx`)+
    `useModelPick.pickModel`(乐观 `applySessionPatch` → `api.patchSession`,失败回滚)。
- 客户端支持已就绪:langchain-anthropic 1.7.7 有 `thinking` 字段;langchain-openai
  1.7.3 有 `reasoning_effort` + `extra_body`(qwen `thinking_budget` 走 extra_body)。
- 内部廉价调用(title_gen / memory.summarize / scheduler `_llm_summary` /
  subagent gate)各自 `build_model`,**不经会话 getter**——天然不受影响,但设计上
  要显式声明它们永远 auto(§4)。

## §1 产品定义

会话新增 5 档思考强度,与 provider/model 并列、同生命周期(创建时设、运行中热切、
fork 复制)。**缺省 auto = 行为与今天完全一致**。

| 档位 | 语义 | anthropic 协议 | openai-responses | openai-compat |
|---|---|---|---|---|
| `auto`(缺省) | 跟随 provider 配置 | 不传 thinking | 不传 reasoning | 不传(配置里 `enable_thinking` 继续生效) |
| `off` | 显式关思考 | `thinking:{type:disabled}` | 最低可用档 | `enable_thinking:false` |
| `low` | 快思考 | budget 4096 | effort low | enable_thinking + 小 budget |
| `medium` | 标准思考 | budget 16384 | effort medium | enable_thinking + 中 budget |
| `high` | 深度思考 | budget 32768 | effort high | enable_thinking + 大 budget |

抽象档位而非裸参数:三家方言不互通(anthropic=token 预算 / OpenAI=语义档 /
GLM=仅开关),档位让「换模型不换习惯」;预算数值可按 provider 覆盖(§3)。

## §2 数据契约

- `SessionMeta` + 会话记录:新增 `thinking_effort: string | null`
  (`"auto"|"off"|"low"|"medium"|"high"`;`null`/缺省 = auto)。存 `_index.json`,
  走 `_session_meta_patch` 现有过滤(None 不落)。
- `POST /api/sessions`(CreateSessionRequest)接受可选 `thinking_effort`;
  `_resolve_provider_model` 同站点解析缺省(未传 → auto)。
- `PATCH /api/sessions/{id}`:`PatchSessionRequest` 增 `thinking_effort: str|None`。
  传入且非空 → 与 provider/model 换绑同路径:重建 model 对象、原地换
  `s["model"]`、meta 落档、广播 `session.model_changed`(payload 增
  `thinking_effort` 字段,其它已打开同会话的客户端据此同步)。
- `build_model(provider, model_name, session_id, thinking_effort=None)` 增参,
  各调用点:会话 getter 路径传会话档位;内部调用点不传(= auto)。
- 前端 `patchSession`/`applySessionPatch` 直接复用(`Partial<SessionMeta>` 已覆盖)。

## §3 协议映射与方言

`models.py` 新增解析器 `thinking_params(cfg, model, effort) -> dict`,按协议:

- **anthropic**:`ChatAnthropic(thinking={"type":"enabled","budget_tokens":B})`;
  off = `{"type":"disabled"}`。约束:开思考时 temperature 强制 1(Anthropic API
  要求)、`budget_tokens < max_tokens` → 档位生效时
  `max_tokens = max(配置值, B + 4096)`。
- **openai-responses**:`reasoning={"effort": "low"|"medium"|"high"}`;
  off → `"minimal"`(o 系/gpt-5 系最低档;不支持的模型端点自己忽略)。
- **openai-compat**:`extra_body` 方言表,按 base_url/模型名判别:
  - Qwen/DashScope 系:`enable_thinking: true` + `thinking_budget`(low 4096 /
    medium 16384 / high 32768);off = `enable_thinking: false`。
  - GLM(bigmodel):`thinking: {"type":"enabled"|"disabled"}`,无预算概念 →
    off=disabled,low/medium/high 全落 enabled(P2 再考虑借 max_tokens 分层)。
  - DeepSeek:reasoner 类模型恒思考、chat 类不思考 → 档位仅透传
    `reasoning_effort`(网关新版支持则生效),否则静默回落。
  - 判别不出 → 仅尽力透传 `reasoning_effort`,不阻断请求。
- **预算覆盖**:provider 配置可选 `thinking_budgets: {low, medium, high}`
  (token 数),缺省用上表内置值。

## §4 生效范围(已拍板)

- ✅ **主对话 turn**(graph agent 节点,经 session getter):生效、热切。
- ✅ **默认档 auto**:新会话/未设置 = 维持现状,零行为变化。
- ❌ **子代理/worker/delegation 子会话:不继承父会话档位**,spawn 不传 →
  子会话 auto(跟随各自 provider 配置)。深研场景想要 high 的,由 agent 的
  spawn 工具带参解决(P2,见 §9)。
- ❌ **内部廉价调用**(title_gen / summarize / scheduler `_llm_summary` /
  subagent gate):永远 auto,不跟随会话档位——起标题不能烧 32k thinking。
- worker bus 跨会话派活:目标会话保持自己的档位(与 model 同语义)。
- 定时任务:P0 读会话保存的档位;任务级覆盖字段 P1(§9)。
- delegation 会话(claude/pi/codex 外部后端):provider/model 只是展示标签,
  档位字段允许存在但无效果,UI 置灰。

## §5 UI(composer 模型 chip 菜单内,已拍板)

- `ModelChipMenu` 弹层在模型列表下方加「Thinking」分组:5 个 radio 行
  (Auto / Off / Low / Medium / High),当前档打勾。选中即
  `patchSession({thinking_effort})`(乐观更新,同 pickModel)。
- chip 本体加小角标:非 auto 时显示档位缩写(off/LO/MED/HI),auto 无角标。
- 首页 composer:同一菜单,`homeModel` 状态扩展
  `{provider, model, thinking_effort}`,`createAndSend` 随创建请求带出。
- 不支持时(§7 能力声明):分组整体置灰 + 一行说明「当前模型不支持思考档位」。
- i18n:en 主文案 + zh 双语,术语进术语表(memory: UI English)。

## §6 热切与多端同步

- 与换模型完全同语义:运行中 turn 当前 LLM 调用继续旧参数,下一 superstep
  用新档;无需断开 WS。
- `session.model_changed` payload 增 `thinking_effort`,已打开同会话的其它
  客户端(浮动窗/移动端)同步 chip 显示。

## §7 能力声明与不支持处理

- provider 配置新增 `thinking_mode: "auto"|"budget"|"effort"|"none"`,缺省
  `auto` = 按协议 + 模型名启发式(见 §3 方言判别)。`none` = UI 置灰。
- 跨协议换模型(chip 里切 provider):档位保留、按新协议重新映射;映射不了
  (如 GLM 只有开关)落到最近档,chip tooltip 说明实际效果。
- 请求被网关拒绝(参数不支持):既有 provider 错误透传路径把原文带回错误卡,
  不静默;用户可回落 auto。

## §8 边界

- **fork**:复制档位(与 provider/model 一并复制)。
- **mid-session 切模型**:同 §7,档位跟随会话不跟随模型。
- **回放/导出**:档位是会话配置不进消息历史;导出 md 头部附一行当前配置(P2)。
- **usage**:thinking tokens 已计入 output(现状);Stats hover 拆分显示 P2。
- **归档**:无交互,档位随会话记录保存。
- **heal 链**(`config.py:595`):只重解析 provider/model 绑定失效的会话,
  档位字段不动(不参与绑定有效性判断)。

## §9 分期

- **P0**:§2 数据契约 + §3 三协议映射 + PATCH 热切 + `session.model_changed`
  扩展 + ModelChipMenu UI + spawn 不传(子会话 auto)+ fork 复制。
- **P1**:agent 级默认档(AgentsSettings,创建时解析链 session > agent > auto)
  + 能力声明置灰(`thinking_mode`)+ 定时任务任务级覆盖 + `/think <档>` 命令
  + GLM 方言完善。
- **P2**:provider 配置 `thinking_budgets` 自定义预算 + spawn 工具带
  `thinking_effort` 参(深研子任务开 high)+ Stats 思考 token 拆分
  + 导出附配置。

## §10 决策记录(2026-11-02 与用户定稿)

1. **档位集**:5 档 off/low/medium/high + auto 缺省(对齐 Claude/OpenAI 官方
   档位;GLM 类只有开关的,off=关、其余落开)。
2. **子会话**:固定 auto,不继承父会话档位(子任务倾向省钱;深研需求走 P2
   spawn 带参)。
3. **UI 入口**:composer 模型 chip 菜单内(与选模型同一心智上下文,零新增
   入口)。
4. **默认档**:auto 维持现状(想要思考的用户主动开,零惊喜)。
