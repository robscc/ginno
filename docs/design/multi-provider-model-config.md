# 多模型 API 配置（Multi-Provider Model Config）设计

状态：设计稿（未实施） · 2026-09-26
范围：`~/.ginno/settings.json` 的 providers 结构 v2、设置页「模型 API」重构、runtime 协议适配层。本文只做设计，不涉及实现。

---

## 1. 现状与痛点

### 1.1 现有槽位结构（精确到字段）

`settings.json` 顶层与 providers 相关的键：

- `providers`：**以 provider id 为键的 dict**（不是数组）— `packages/runtime/src/ginno_runtime/providers.py:5-15`
- `default_provider`：顶层字符串，全局默认 provider id — `providers.py:166-176`

三个内置槽由 `PROVIDER_IDS = ("anthropic", "openai", "custom")` 定义（`providers.py:31`），每个槽的字段来自 `PROVIDER_DEFAULTS`（`providers.py:33-69`）：

| 槽 id | protocol | 专属字段 | 备注 |
|---|---|---|---|
| `anthropic` | `anthropic` | `api_key, default_model, base_url, max_tokens, temperature, timeout_s, enable_search` | `enable_search` 实际对 anthropic 无效（models.py 只在 openai 分支读取） |
| `openai` | `openai` | `api_key, default_model, base_url(默认 https://api.openai.com/v1), org_id, max_tokens, enable_search, enable_thinking` | `org_id` 只存在于 schema，从未传给 SDK |
| `custom` | `openai-compatible` | `name, api_key, base_url, model(注意：不叫 default_model), max_tokens, temperature, timeout_s, enable_search, enable_thinking` | 「custom 槽」= 自定义 base_url 的 OpenAI 兼容端点（Ollama/DeepSeek/Qwen 等），**只有一个** |

公共字段：`enabled`（启停开关）。

读写路径：

- `load_providers()`（`providers.py:137-150`）：三个内置 id 逐个 `deepcopy(PROVIDER_DEFAULTS[pid])` 再 merge 存储值；**额外 id 会被原样保留**（`providers.py:147-150`），但拿不到 defaults。
- `save_providers()`（`providers.py:153-163`）：整块替换 `settings["providers"]`，已知 id merge defaults。
- HTTP API：`GET /api/providers`（`api/config.py:184-194`）、`PUT /api/providers`（`api/config.py:254-278`，**全量 dict 整块替换**，成功后 `_SESSIONS.clear()` + `_refresh_session_metas()`）；verify 与 search_probe（`api/config.py:281-293`）。

### 1.2 「只能 3 个」在哪硬编码

- **真正的上限在前端**：`apps/web/src/components/settings/ModelApiSettings.tsx:10` 的 `ORDER = ["anthropic", "openai", "custom"]`，`:203` 用 `ORDER.filter((id) => draft[id])` 渲染 —— 只有这三张卡片，没有任何「新增」入口。
- 后端并没有强制 3 个（`load_providers` 保留额外 id），但第 4 个 id 处处享受二等公民待遇：
  - `get_default_provider()` 的启用 fallback 循环只遍历 `PROVIDER_IDS`（`providers.py:172`）—— 额外 id **永远不会**被自动选为默认；
  - `PROVIDER_DEFAULTS` 没有它的条目，字段缺省行为不确定。
- 前端类型 `ProviderConfig`（`apps/web/src/lib/types.ts:78-99`）与 `Providers = Record<string, ProviderConfig>`（`types.ts:101`）是开放 dict，类型上不限制数量。

### 1.3 命名混乱点

1. **provider id vs 显示名**：`custom` 槽有可选 `name` 字段（`providers.py:59`），前端作为「端点名称」输入（`ProviderCard.tsx:241-248`）；anthropic/openai 连 name 都没有，卡片标题硬编码在 `META`（`ModelApiSettings.tsx:11-39`）。usage 统计里 provider 维度直接存/聚合这个 **id 字符串**（`api/stream.py:1793`、`usage_store.py:244-250`），用户在用量页看到的是 `custom` 而不是自己起的名字。
2. **默认模型字段双名**：anthropic/openai 用 `default_model`，custom 用 `model`（`providers.py:38,49,62`）。`model_for_provider()` 做了兜底兼容（`providers.py:179-181`），但 `build_model_by_name()` 的 DSL `extract_model` 名字解析只精确匹配 `cfg.get("model")`（`models.py:187`）—— 对 anthropic/openai 槽的 `default_model` **不生效**，已经是一个现存的坑。
3. **协议 vs 槽位耦合**：用户视角「OpenAI」既是一个槽也是一个协议；`openai` 与 `openai-compatible` 两个协议在后端走的是**同一份构建代码**（`models.py:144-173`），差异仅是默认 base_url 与 extra_body 开关。

### 1.4 协议覆盖现状

`build_model()` 按 `cfg["protocol"]` 分发（`models.py:117-173`）：

- `anthropic` → `langchain_anthropic.ChatAnthropic`，支持 `base_url`、`bearer_auth`（第三方网关用 `Authorization: Bearer` 替代 `x-api-key`，`models.py:140-141`）。
- `openai` / `openai-compatible` → `ChatOpenAI`（实际是保留 `reasoning_content` 的子类 `ReasoningChatOpenAI`，`models.py:37-78`）；compatible 端点支持 `extra_body` 私有开关 `enable_search` / `enable_thinking`（`models.py:165-172`）。
- **OpenAI Responses API（`/v1/responses`）完全不支持。**

每个配置只能配**一个**模型（`default_model`/`model` 单值字段），没有模型列表、没有「拉取模型列表」能力（verify 里 `client.models.list()` 只当探活用，`providers.py:231-233`）。

### 1.5 验证（Test）现状

`providers.verify()`（`providers.py:184-241`）：

- anthropic：1-token `messages.create`；openai 系：先 `client.models.list()`，失败再降级 1-token chat（兼容部分没有 `/models` 的端点，`providers.py:230-238`）。
- 结果 `{ok, error?, latency_ms}` **不落盘**，只存在于前端 `VerifyState`（`ProviderCard.tsx:7-11`），重启即失。
- 验证跑的是**已保存**的配置：前端必须先保存再验证（`ModelApiSettings.tsx:110-116`），填了 key 未保存时验证的是旧值。

### 1.6 provider 引用面（谁拿着 provider id 字符串）

这是本次改造最大的风险面，逐处列出：

| 引用处 | 位置 | 语义 |
|---|---|---|
| 会话创建解析 | `api/sessions.py:75-99` `_resolve_provider_model` | 候选 `req.provider → req.model_provider → agent.provider → 全局默认`，取第一个 enabled 的；解析结果**持久化进 session meta**（`sessions.py:537,566`） |
| 会话 patch / rebuild | `api/sessions.py:610-641, 868-873` | 按保存的 meta provider 重新 `build_model`；provider 改了没给 model 时取该 provider 的默认模型 |
| 保存 providers 后的批量重解析 | `api/config.py:202-251` `_refresh_session_metas` | 所有 session meta 按「enabled 的 agent provider，否则 enabled 的全局默认」重算；**失引/停用会静默回落到默认**而不是报错 |
| Agent 配置 | `agents/registry.py:39-40` | `AgentConfig.provider: str = "custom"`、`model: str = ""`；三个 seed agent 全部默认 `custom`（`registry.py:120,129,141,178`） |
| Workflow fork agent | `api/workflows.py:791` | `build_model(fork.provider, fork.model or None)`；usage 归属同样带 fork 的 provider/model（`workflows.py:797-800`） |
| DSL extract 节点 | `workflows/nodes/extract.py:109-112` | `build_model_by_name()`（`models.py:176-192`）：custom 的 `model` 字段精确匹配 → provider id 命中 → 默认 provider + 名字当模型 |
| 标题生成 | `title_gen.py:121` | `build_model(meta.provider, meta.model)` |
| 记忆蒸馏 | `memory/summarize.py:224` | `build_model(chosen)` |
| Usage 归属 | `api/stream.py:1792-1803`、`usage_store.py:72-95,244-250`、`api/usage.py:43,54,62` | 按 session reg 的 `model_provider`/`model_name` 字符串落账、聚合、筛选 |
| 前端 | `AgentsSettings.tsx:84,265-268` | agent 编辑表单的 provider 下拉直接 `Object.entries(g.providers)`；未启用时黄条警告（`:179-184`） |

---

## 2. 产品设计

### 2.1 配置列表页信息架构

「设置 → 模型 API」改为**配置列表 + 新增**，不再固定三张卡。每个配置卡片：

```
┌────────────────────────────────────────────────────────────┐
│ [协议徽标] 名称（用户起）            [默认] [启用 ⬤] [⌄]    │
│ 协议 · base_url 域名 · N 个模型 · 最近验证 ✅ 2 分钟前 / ❌ │
│ 模型 chips: qwen-plus* · deepseek-v3 · +                   │  * = 该配置默认模型
└────────────────────────────────────────────────────────────┘
```

- 卡片头部：名称、协议徽标（Anthropic / OpenAI Compatible / OpenAI Responses 三色）、默认标记、启用开关、展开编辑。
- 摘要行：协议 + base_url 域名 + 模型数 + **最近验证状态与时间**（`verified_at` 落盘后可显示「3 天前验证」，超 7 天弱化为「建议重新验证」）。
- 模型行：chips 展示 `models[]`，其中默认模型打星；点击 chip 可在会话创建/agent 编辑里直接选用「配置+模型」组合。
- 排序：默认配置置顶，其余按名称。

### 2.2 新增配置流程

四步向导（也可单页表单分区，视实现成本定）：

1. **选协议**：三选一卡片 —— Anthropic 协议 / OpenAI Compatible（自定义 base_url 的国产模型、中转、本地部署都归这类，文案需明示）/ OpenAI Response API。
2. **填连接信息**：name（必填，用户可读名）+ base_url + api_key（按协议不同，见 2.3）。
3. **配模型**：拉取（P3：`GET {base_url}/models`，Anthropic 为 `GET /v1/models`）或手动逐个添加模型 id；勾选其中一个为该配置默认模型。
4. **Test → 保存**：验证按钮直接用**当前草稿**发起（见 §3.4 verify_draft），成功显示延迟；保存后卡片入库。

### 2.3 三种协议的表单差异

| 字段 | Anthropic | OpenAI Compatible | OpenAI Responses |
|---|---|---|---|
| 名称 | ✅ | ✅ | ✅ |
| Base URL | 可选（默认官方） | **必填** | 可选（默认 `https://api.openai.com/v1`） |
| API Key | 必填 | 可选（本地端点留空） | 必填 |
| 认证方式 | `x-api-key`；`bearer_auth` 复选（第三方网关） | `Authorization: Bearer` | `Authorization: Bearer` |
| 模型列表 | 多个模型 id | 多个模型 id（可拉取） | 多个模型 id（可拉取） |
| max_tokens / temperature / timeout | ✅ | ✅ | ✅（max_tokens 映射为 `max_output_tokens`） |
| enable_search / enable_thinking | 不显示 | ✅（高级区，兼容端点私有 body 参数） | 不显示（Responses 有原生 web_search 工具，P3 再评估） |
| org_id | — | — | ✅（官方 OpenAI 可选） |

共用字段尽量同名同义，`protocol` 字段决定渲染分支 —— 前端把 `ProviderCard.tsx` 的 `isCompat` 二分支改为三分支协议映射表。

### 2.4 默认配置语义

- **全局默认**：唯一，语义不变（顶层 `default_provider`，指到新配置 id）。`get_default_provider()` 的解析顺序保持「显式设置 → 任一 enabled 配置 → 原值」，但 fallback 循环改为遍历**全部**配置（修掉 `providers.py:172` 只看三槽的问题）。
- **agent 覆盖**：agent 保存 `(provider_id, model)` 二元组，语义不变（`sessions.py:87-97` 的优先级链保持）。agent 编辑页的 provider 下拉改读新配置列表，model 下拉跟随所选配置的 `models[]`（允许手输兜底）。
- **会话级**：保持现有 meta 持久化 + patch 切换语义（`sessions.py:610-641`），不改。
- 停用默认配置：允许，但置顶黄条「当前默认已停用，新会话将回退到 X」并自动把默认切到第一个 enabled 配置（与 `_refresh_session_metas` 的回落行为一致）；不弹阻断框。

### 2.5 删除/停用有引用时的行为

引用探测范围：agents（`agent.provider`）、session metas（`meta.provider`）、workflow DSL（`build_model_by_name` 命中过）。删除确认框列出示数：「3 个 Agent、12 个会话正在使用」。

- **停用**：不阻断。新会话走回落链（现有 `_resolve_provider_model` 的 `_enabled` 过滤已支持，`sessions.py:80-92`）；旧会话 rebuild 时 `build_model` 抛「disabled」（`models.py:114-115`），TopBar 可见，用户可切换。
- **删除**：阻断式确认（输入名称确认可选）。执行时：
  1. 若是全局默认 → 默认切到第一个 enabled；
  2. agents 引用它 → 保留 agent.provider 原字符串但编辑页显示「已删除」红字（不做静默改写，用户明确选择新值；与 `AgentsSettings.tsx:182-184` 的未启用警告同思路，升级为已删除）；
  3. 历史会话 meta 不改写 —— 老会话保持原样可回看，继续运行时按回落链解析（`_refresh_session_metas` 现有行为，`config.py:230-240`）；
  4. usage 历史记录**永不删改**（存量字符串照常聚合，见 §3.6）。

### 2.6 迁移体验

对用户**零感知**：启动时自动完成，无弹窗、无引导页。三个旧槽无损变为三个具名配置，id 不变（`anthropic`/`openai`/`custom` 保留为合法配置 id，见 §3.2），所有 agent/session/usage 引用自动有效。`custom` 槽的 `name` 为空时迁移补默认名「自定义端点」。回滚安全性见 §3.3。

---

## 3. 技术设计

### 3.1 settings.json v2 schema

```jsonc
{
  // ... 其它顶层键不变 (use_system_proxy 等)
  "default_provider": "prov_main",            // 指向 enabled 配置的 id
  "providers": [
    {
      "id": "prov_main",                       // 新配置: "prov_" + 随机 8 位; 见 §3.2 迁移 id 规则
      "name": "中转站 A",                      // 用户可读名, 必填
      "protocol": "openai_compatible",         // anthropic | openai_compatible | openai_responses
      "base_url": "https://relay.example.com/v1",
      "api_key": "sk-...",
      "bearer_auth": false,                    // 仅 anthropic 协议有意义
      "org_id": "",                            // 仅 openai_responses / openai 官方
      "models": ["qwen-plus", "deepseek-v3"],
      "default_model": "qwen-plus",            // 必须 ∈ models
      "max_tokens": 8192,
      "temperature": 0.7,
      "timeout_s": 60,
      "enable_search": false,                  // 仅 openai_compatible
      "enable_thinking": false,                // 仅 openai_compatible
      "enabled": true,
      "verified_at": 1758860000,               // unix 秒; null/缺省 = 从未验证
      "created_at": 1758860000
    },
    {
      "id": "anthropic",                       // 迁移来的内置 id 原样保留
      "name": "Anthropic",
      "protocol": "anthropic",
      "api_key": "sk-ant-...",
      "base_url": "",
      "models": ["claude-sonnet-4-5"],
      "default_model": "claude-sonnet-4-5",
      "max_tokens": 4096,
      "temperature": 0.7,
      "timeout_s": 60,
      "bearer_auth": false,
      "enabled": true,
      "verified_at": null,
      "created_at": 1758860000
    }
  ]
}
```

要点：

- `providers` 从 **dict 变数组**；`id` 是稳定主键，`name` 仅展示。协议枚举从 `openai`/`openai-compatible` 归一为 `anthropic | openai_compatible | openai_responses`（kebab→snake，与 Python 侧判断对齐；前端展示层转写）。
- `models[]` 取代单值模型字段；`default_model` 必须是 `models` 成员。`custom.model` 兼容期由读取层折叠进 `models[0]`。
- `verified_at` 落盘，verify 成功时由 runtime 写回。

### 3.2 迁移：读旧写新 + 回滚安全

**触发**：`paths.ensure_layout()` 里现有 `_migrate_settings`（`paths.py:170-217, 244-253`）之后追加一步 `_migrate_providers_v2`，幂等：

- `providers` 是 dict → 视为 v1：
  1. 整个 dict 原样快照进顶层新键 `providers_v1`（**不删**）；
  2. 按 `PROVIDER_IDS` 顺序转换成数组：id 保持 `anthropic`/`openai`/`custom`；`openai` 协议映射为 `openai_compatible`（官方端点与兼容端点构建代码本就同路，`models.py:144-173`；`base_url` 为官方默认值时前端徽标显示 OpenAI）；
  3. `custom.model` → `models:[model]` + `default_model`；`anthropic/openai.default_model` → `models:[default_model]`；
  4. `custom.name` 为空 → `"自定义端点"`；
  5. `verified_at: null, created_at: now`；
  6. 写回 `settings["providers"]`（数组）+ `settings["providers_v1"]`（快照）。

**读兼容**：`load_providers()` 对 dict 形态的存量数据继续走 v1 路径再转换（防御：用户手动改文件/迁移中途崩溃），对数组形态直接用。

**回滚安全**：旧版 Ginno 二进制的 `load_providers` 会对数组执行 `stored.get(pid)`（`providers.py:140-144`）直接崩溃 —— 因此**绝不能**无损回滚，只能：`providers_v1` 快照保留，文档提供「恢复脚本/说明」（用 `providers_v1` 覆盖 `providers`、删 `providers_v1`）。`ensure_layout` 只在**首次**迁移时写快照（`providers_v1` 已存在则跳过），避免旧版回滚后再升级把「已被新结构污染」的数据二次快照。

> 备选方案（保守）：不动 `providers`，新结构存新键 `model_configs`，旧键冻结为只读镜像。优点是旧版二进制零风险；缺点是双键长期共存、`PUT /api/providers` 语义分裂。**列入开放问题 Q1**。

### 3.3 协议适配层（models.py 改造点）

`build_model(provider_id, model_name)` 签名不变（全部调用点 —— `sessions.py:489,641,873`、`workflows.py:791,560,711`、`title_gen.py:121`、`summarize.py:224`、`extract.py:112` —— 均无需改签名），内部分发从「if anthropic / else openai」改为每协议一个 adapter：

```
build_model
  ├─ load_config(provider_id)        # providers.py 新函数: 数组里按 id 查 + enabled 校验
  ├─ model = model_name or cfg.default_model
  └─ ADAPTERS[cfg.protocol](cfg, model, sampling)
       ├─ anthropic          → ChatAnthropic(...)            # 现 models.py:122-142 原样搬入
       ├─ openai_compatible  → ReasoningChatOpenAI(...)      # 现 models.py:144-173 原样搬入
       │                       (extra_body enable_search/enable_thinking 逻辑随迁)
       └─ openai_responses   → ChatOpenAI(use_responses_api=True, ...)   # P2
```

- `langchain-openai` 已锁 1.3.5（`packages/runtime/uv.lock:1058-1060`），`ChatOpenAI` 原生支持 `use_responses_api=True` 走 `/v1/responses`，LangChain 消息接口不变，**主链路（stream/graph/usage 钩子）零改动** —— 这是 P2 低成本落地的关键。
- `model_for_provider()`（`providers.py:179-181`）保留，内部改为读 `default_model`。
- `build_model_by_name()`（`models.py:176-192`）匹配规则升级为：① enabled 且 `model_name ∈ cfg.models` 的配置 → ② provider id 命中 → ③ 默认配置 + 名字。顺带修掉只匹配 `cfg["model"]` 的坑（§1.3-2）。

**Responses API 语义差异映射表**（adapter 内注释同款，实现时对照）：

| 维度 | chat completions | Responses API |
|---|---|---|
| 端点 | `POST /v1/chat/completions` | `POST /v1/responses` |
| 文本流事件 | `choices[].delta.content` | `response.output_text.delta` |
| 推理流 | `choices[].delta.reasoning_content`（非官方扩展，`models.py:66-74` 依赖它） | `response.reasoning_summary_text.delta`（官方 reasoning summary）；`ReasoningChatOpenAI` 的提取逻辑**不适用**，需在 adapter 里把 Responses 的 reasoning 块重新写进 `additional_kwargs["reasoning_content"]`，保住 `api/stream.py` 的 `thinking.delta` 管道与 `messages_ui` 回放 |
| 工具调用 | `delta.tool_calls` | `response.output_item`（`function_call` item）+ `response.function_call_arguments.delta` |
| usage 载荷 | 末 chunk `usage`（需 `stream_options.include_usage`） | `response.completed` 事件的 `response.usage`：`input_tokens/output_tokens`、`input_tokens_details.cached_tokens`、`output_tokens_details.reasoning_tokens` |
| usage 归一 | `usage.py` 的 `_normalize` 已按 langchain 标准字段工作 | langchain 层已归一；`reasoning_tokens` 归入 `additional_kwargs`，P2 暂不单独入库（避免 usage schema 变更），列入 P3 |

### 3.4 verify / Test 改造

- `providers.verify(provider_id)` 保留（旧配置验证），新增 `POST /api/providers/verify_draft`：请求体携带完整配置草稿（含 api_key），**不落盘**直接探测 —— 解决「必须先保存才能验证」（`ModelApiSettings.tsx:110-116`）的体验问题。三协议探测方式：anthropic 1-token messages；compatible 沿用 list→chat 降级；responses 用 `client.responses.create(max_output_tokens=16)` 最小调用。
- 成功后 `PUT /api/providers` 时由前端带上刚拿到的状态，或 verify_draft 成功即保存（产品取其一，倾向后者）。
- `search_probe`（`providers.py:244-281`）不变，仍按配置 id 构建。

### 3.5 settings 读写与 PUT 语义

- `PutProvidersRequest.providers: dict`（`api/config.py:197-199`）→ `list[dict]`；后端逐条校验（id 唯一、default_model ∈ models、协议合法），生成缺失 id。
- 保存后仍走 `_SESSIONS.clear()` + `_refresh_session_metas()`（`config.py:270-277`）—— 多配置下该函数逻辑不变，只是 `providers.get(pid)` 改为数组查找。
- 前端「全量 PUT」模式在多配置下并发冲突风险变高（两个设置页互踩）。P1 接受现状（单用户桌面应用），P3 可改增量端点（POST/PUT/PATCH 单配置）。

### 3.6 usage 归属与 fork 链

- **零 schema 变更**：usage 记录继续存 provider **id 字符串**（`usage_store.py:72-95` 字段不动），存量数据天然有效（迁移后内置 id 不变）。
- 展示层：`api/usage.py:43,54` 与前端用量页增加 id→name 映射（GET /api/providers 已可取），把 `custom` 渲染为「自定义端点」；聚合键仍是 id。
- workflow fork（`workflows.py:791-800`）与 title_gen/summarize 不需要改：都经 `build_model(provider_id, ...)`，适配层内部消化协议差异。
- 已删除配置的 usage 历史字符串照常聚合（id 失去 name 映射后显示原 id）。

### 3.7 前端改造点

- `types.ts:78-101`：`ProviderConfig` 增 `id/name/protocol(三值)/models[]/default_model/verified_at/created_at`；`Providers` 改 `ProviderConfig[]`。
- `ModelApiSettings.tsx`：删除 `ORDER`/`META` 硬编码（`:10-39`），改为读接口列表渲染卡片数组 + 「新增配置」按钮 + 新增/编辑表单（按 §2.3 协议分支）；保存从「每次 blur 全量 PUT」改为编辑态集中保存。
- `ProviderCard.tsx`：`isCompat` 二分支 → 三协议映射；增加模型 chips 编辑器与「从端点拉取模型」按钮（P3）。
- `AgentsSettings.tsx:265-268`：provider 下拉 + model 下拉联动（model 选项来自所选配置 `models[]`，允许自由输入）。
- 会话创建/TopBar 的模型选择：升级为「配置 → 模型」二级选择（保持现有 `provider`+`model` 两个参数不变，纯 UI 改）。

---

## 4. 分阶段落地

**P1 — 结构与双协议打通（核心价值）**
1. settings v2 schema + `_migrate_providers_v2`（含 `providers_v1` 快照与读兼容）；
2. `providers.py` 数组化（load/save/get_default_provider/verify）+ `build_model` adapter 化（anthropic、openai_compatible 两个 adapter，纯搬移）；
3. `POST /api/providers/verify_draft` + `verified_at` 落盘；
4. 前端列表页重构 + 三协议表单（Responses 表单先出、后端 P2 才支持，置灰或直接允许配置 P2 生效）；
5. 删除/停用确认与引用提示；AgentsSettings 下拉接新列表。
   验收：老 settings.json 启动无损迁移；三个旧 id 的既有会话/agent/usage 全部不变；新建 ≥2 个 compatible 配置并分别 Test 通过。

**P2 — Responses API adapter**
1. `openai_responses` adapter（`use_responses_api=True`）+ verify 分支；
2. reasoning summary → `additional_kwargs["reasoning_content"]` 桥接，保住 thinking.delta 管道；
3. e2e：官方 OpenAI + 一个 Responses 兼容网关（如有）跑通流式/工具调用/usage 记账。

**P3 — 体验打磨**
1. 模型列表拉取（`GET {base_url}/models`，anthropic `/v1/models`）；
2. usage 页 id→名称映射、按模型计价展示（内置价目表 + 自定义单价）；
3. 增量保存 API（避免全量 PUT 互踩）；`reasoning_tokens` 入 usage；
4. Responses 原生 web_search 工具评估（替代 compatible 的 `enable_search` 私参）。

---

## 5. 开放问题（需产品决策）

1. **Q1 落盘方案**：`providers` 原地变数组 + `providers_v1` 快照（本文主案，回滚靠脚本）vs 新键 `model_configs` + 冻结旧键（回滚零风险，但双键共存）。取决于是否需要支持「装回旧版本」作为正式场景。
2. **Q2 内置 id 去留**：迁移后 `anthropic`/`openai`/`custom` 三个字符串 id 保留为普通配置 id（本文推荐，兼容成本最低）还是统一换成 `prov_*`（更整洁，但 session meta / agent / usage 存量引用需要一次性改写）。
3. **Q3 agent 的 model 绑定形态**：自由文本（现状）还是强约束为所选配置 `models[]` 的成员？强约束更防错，但兼容端点模型名常有别名/变体。
4. **Q4 verify 成功是否即保存**（§3.4 末）。
5. **Q5 兼容端点私有开关**（`enable_search`/`enable_thinking`）保留在高级区即可，还是要按 base_url 域名自动推荐（如 *.dashscope.aliyuncs.com 自动勾选）？
6. **Q6 协议枚举命名**：`openai_compatible`（snake）还是沿用存量字符串 `openai-compatible`？后者可少一层映射，但与新增枚举风格不一致。
7. **Q7 API Key 明文存储**：现状 `settings.json` 明文（`providers.py:131-134`）。是否本次一并做 keychain/DPAPI 加密？建议独立立项，不阻塞本设计。
8. **Q8 删除默认配置时**的确认强度：黄条确认即可，还是要求输入名称？
9. **Q9 配置数量是否真设上限**：需求说「任意多」，是否仍设软上限（如 20）防误批量导入？建议不设，仅分页/折叠。

---

## 附：现状引用速查

| 事实 | 位置 |
|---|---|
| 三槽定义 | `packages/runtime/src/ginno_runtime/providers.py:31` |
| 槽默认字段 | `providers.py:33-69` |
| 前端 3 张卡硬编码 | `apps/web/src/components/settings/ModelApiSettings.tsx:10,203` |
| 默认 fallback 只看三槽 | `providers.py:172` |
| 协议分发（anthropic / openai 系） | `packages/runtime/src/ginno_runtime/models.py:122-142,144-173` |
| reasoning_content 保留子类 | `models.py:37-78` |
| verify 逻辑 | `providers.py:184-241`；前端先存后验 `ModelApiSettings.tsx:110-116` |
| PUT 全量替换 + session 重解析 | `packages/runtime/src/ginno_runtime/api/config.py:254-278,202-251` |
| 会话创建解析/持久化 | `api/sessions.py:75-99,537,566` |
| agent.provider 默认 custom | `agents/registry.py:39-40,120` |
| workflow fork 用 provider | `api/workflows.py:791-800` |
| usage 按 provider 字符串落账/聚合 | `api/stream.py:1792-1803`、`usage_store.py:72-95,244-250` |
| 旧 settings 迁移先例 | `paths.py:170-217,244-253` |
| langchain-openai 版本（支持 Responses） | `packages/runtime/uv.lock:1058-1060`（1.3.5） |
