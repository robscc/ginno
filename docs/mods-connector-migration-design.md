# Mods × 连接器：统一扩展基座 + 差异化详情视图

- 日期：2026-10-23
- 状态：设计稿（待实现）
- 前置：`claude-code-mods-design.md`、`mod-marketplace-design.md`、`connector-module-design.md`
- 原型：`docs/mods-config-editing-redesign.html`

## 0. 决议

| 项 | 决定 |
|---|---|
| 迁移范围 | **L1 + L2**：产品面统一 + 连接器底座契约统一；**存储暂不迁移**（mod 仍存 `settings.mods.items`） |
| 统一入口名 | **连接器** |
| Mods 分组名 | **Claude Mods**（它们就是 Claude Code 兼容的 mod；代码标识、settings 键、API 路径仍用 `mods`，不做数据层重命名） |
| 优先级 | ① 配置编辑器 ② 权限与信任 ③ 健康与诊断 ④ 会话内呈现面 |
| 外观 | **统一基座，不统一外观**：每个连接器声明自己的 tab 与区块，共享渲染器与数据契约 |

## 1. 现状（代码事实，设计依据）

### 1.1 配置
- `ModsSettings.tsx:409-430`：每个 mod 一个 6 行 `<textarea>`，初始值 `JSON.stringify(m.config ?? {})`。
- 默认值不在 API 里：`GET /api/mods` 只返回 settings 里的 `config`（`api.py:68-83`）；真实默认值在 runner 的 `userConfig`，只于加载时 `{...userConfig, ...pushed}` 合并（`mod-runner/src/index.ts:76-99`）。新装 mod 打开是 `{}`。
- 校验只有 `JSON.parse`（`ModsSettings.tsx:149-157`）；保存是全量替换（`api.py:117-125`）；`apply-config` fire-and-forget，重载失败只进服务端日志（`api.py:128-140`）。
- 成功提示与失败共用同一个元素，且该元素恒为红色（`ModsSettings.tsx:161,448`）。
- 没有 schema / 类型 / 分组 / 默认值 / 重置 / 未保存保护 / secret 掩码。

### 1.2 授权
- JS 四类 grant：`fs.write`(bool)、`http.fetch`(hosts)、`process.run`(argv)、`env.get`(names)（`crates/mod-broker/src/grants.rs`）。
- classic hooks：`grants.classic = allow|deny|ask`（默认 ask），只拦危险类别 install / settings-write（`hooks/grants.py`）。
- **JS grants 在前端没有任何控件**（`ModsSettings.tsx:326-345` 只有 classic 下拉）；而拒绝文案让用户“去 Mods 设置页改”。
- **grants 不是沙箱**：只约束跨 broker 的 `$` 调用；runner 内的 mod 代码可以 `import node:fs`。UI 必须如实表述。

### 1.3 诊断
- `POST /api/mods/validate` 转发 `broker.validate`，但 socket 分发只有 `broker.status / apply-config / answer`（`crates/mod-broker/src/runtime.rs:245-261`）→ **按钮必坏**。
- broker 的 `unserved` 事件清单到了 API 行但前端从不渲染；`mod.report` 只消费两种模式；重启计数/退避/最后加载时间不可见；`~/.ginno/logs/mod-<name>.log` 没有入口。

### 1.4 会话内呈现
- band 里 Input/Select 的值被 `ModBand.tsx:67` 丢掉（只有 pane 支持值）；band 对用户暴露 `#generation`；同一会话的 mod ask 互相覆盖（`handle.ts:1186`）。

## 2. 可独立设计的面

| 面 | 独立设计 | 归属 |
|---|---|---|
| 配置编辑 | ✅ 本期 P0 | 连接器共享 `ConfigEditor` |
| 权限与信任 | ✅ P0（安全） | 连接器底座 `permissions` + 首次运行确认 |
| 健康与诊断 | ✅ P1 | 连接器详情 `activity` tab + broker validate |
| 商店/安装信任 | 已有设计稿 | 与权限联动（安装前预览） |
| 运行时就绪 | 小面 | 页面顶部健康条 |
| 会话内呈现面 | 半独立 | 聊天面，不属设置 |
| 注册物（tools/commands/agents） | 并入概览 | 详情 `overview` |
| state/store | 并入诊断 | 详情 `activity` |

## 3. 连接器底座扩展（L1+L2）

### 3.1 连接器 = 动态实例
```python
class Connector:
    id: str
    kind: Literal["builtin", "mod"] = "builtin"
    dynamic: bool = False          # mod 实例：随安装/卸载注册/注销
```
- `ConnectorRegistry.register/unregister` 允许动态增删；动态 id 必须过**注册闸门**（`read_config` 对未注册 id 的 `enabled=True` 兜底是已知坑，Vibe Island 连接器踩过）。
- 适配器 `ModConnector(Connector)` 每个已安装 mod 一个实例：
  - `name/description/version` ← manifest
  - `config_defaults()` ← runner 上报的 `userConfig`
  - `config_schema()` ← `userConfigSchema`（可选）或按默认值推断
  - `apply_config(cfg)` ← 写 settings → 下发 broker → **等待重载结果**
  - `status` ← broker 状态映射（见 3.3）

### 3.2 配置描述符（统一契约）
```python
# 新增端点返回值
{
  "config":    {...},   # 用户覆盖（现状 settings 里的 config）
  "defaults":  {...},   # mod userConfig / connector default_config()
  "effective": {...},   # defaults ∪ config（展示用；实际合并仍由 runner 做）
  "configSchema": {     # JSON Schema 子集 + x-ginno-* 扩展
     "type": "object",
     "properties": {
       "units": {"type": "string", "enum": ["metric","imperial"],
                 "title": "单位", "description": "…", "default": "metric",
                 "x-ginno-group": "显示", "x-ginno-order": 10},
       "apiKey": {"type": "string", "x-ginno-secret": true, "default": ""}
     }
  }
}
```
- schema 来源三级：`userConfigSchema`（mod 可选声明）→ 从 `userConfig` 默认值推断（bool/number/string/array/enum）→ 未知键进「高级 JSON」。
- 推断规则：`bool→switch`、`number→number`、`string→text`、`string[]→tags`、单值枚举（默认值 + 常见值）→select。
- 后端需要在 `runner.loaded` 帧里携带 `userConfig`（以及可选的 `schema`），由 broker 暴露给 `ModChannel`，再进详情接口。**这是 L2 的必要改动**。

### 3.3 状态映射
| broker | connector |
|---|---|
| `loaded` | `connected`（detail = 最近一次重载时间） |
| `error` | `error`（detail = runner 错误尾巴） |
| `restarting` | **新增 `restarting` 状态**（不建议伪装成 installing） |
| `disabled` | `disabled` |
| 无状态 | `not_installed`（仅内置）/ `disabled`（mod） |

### 3.4 apply_config 必须可等待
```python
async def apply_config(self, cfg: dict) -> dict | None:
    return {"ok": True, "reloaded": True, "error": None}
```
- `PATCH /api/connectors/{id}/config` 返回该结果；前端据此显示「正在重载 → 已应用 / 失败原因」。
- mod 侧需要 broker `apply-config` 的 ack（当前 fire-and-forget）——Rust 改动点。

### 3.5 详情视图声明（差异化外观）
```python
def detail_view(self) -> dict:
    return {
      "tabs": [
        {"id": "overview",  "kind": "overview",  "title": "概览"},
        {"id": "install",   "kind": "install",   "title": "安装向导"},
        {"id": "settings",  "kind": "settings",  "title": "设置"},
        {"id": "activity",  "kind": "activity",  "title": "连接状态"},
      ],
      "actions": [{"id": "reveal_folder", "title": "在 Finder 中显示"}],
    }
```
- 共享 kind：`overview / settings / install / permissions / activity`。
- 自定义页签用**声明式区块**（`{"kind":"custom-sections","sections":[...]}`），不引入任意前端代码。
- 内置连接器各自声明；mod 默认 `overview/settings/permissions/activity`（无配置则去掉 settings）。

## 4. 配置编辑器（第一个交付）

### 4.1 结构
- 入口：连接器卡片 → 详情「设置」tab（无配置则不显示该 tab）。
- 表单：按 `x-ginno-group` 分组；字段行 = 标题/说明 + 控件 + 默认值 + 「已修改」+ 恢复默认。
- 控件：switch / select / number(带范围) / text / password(掩码+显示) / tags(逐行或 chips) / object(子 JSON)。
- 校验：类型、范围、枚举、必填；错误定位到字段。
- 高级 JSON：可折叠的逃生口，双向同步，带校验按钮。
- 保存条：`N 项已修改` / 放弃 / 保存；保存后 `正在应用…` → `已应用` / 失败原因（可重试）。
- 未保存离开保护。
- 无配置 mod：显示「这个 mod 没有可配置项」，不展示空 JSON。

### 4.2 保存语义
1. 写 settings（全量 config）。
2. 下发 broker → mod 重载。
3. 等待 ack；成功/失败都落到 UI 与活动日志。
4. 配置里出现 host/path 类字段时，提示同步权限（见 5）。

### 4.3 秘密字段
- 现状：config 明文存 `settings.json`，且 `GET /api/mods` 原样返回。
- 本期：UI 掩码 + 标记「敏感」，**不改存储**。是否引入系统钥匙串/独立 secret store 单列决策（见 §8-4）。

## 5. 权限与信任（第二个交付）

- 连接器底座新增可选 `permissions_schema()`；`GET/PATCH /api/connectors/{id}/permissions`。
- Mod 的 permissions 映射到 `mods.items.<name>.grants`（L1+L2 不动存储）。
- 页面：
  - `fs.write` 开关；`http.fetch` 域名 chips；`process.run` argv 白名单（危险前缀提示）；`env.get` 名单。
  - classic：危险命令逐条预览 + `allow/ask/deny` 三态。
  - **明确文案：这是能力策略，不是进程沙箱。**
- 首次运行确认：安装/首次启用时，用 validate 报告（事件、`$` 调用）推导所需能力，逐项确认。
- 配置联动：设置页改 host 时提示加入 `http.fetch`。
- 拒绝历史进「活动」tab。

## 6. 健康与诊断（第三个交付）

- 修复 `broker.validate`：在 socket 分发实现（或复用 CLI 的结果结构），返回结构化报告。
- 「活动」tab：加载/重载/重启（次数与退避）/拒绝调用/unserved 事件/日志尾巴。
- 操作：重新加载、重启 broker（仅 dev/web）、打开日志、复制诊断信息。
- 运行时就绪：页面顶部一行（Node 版本、broker 状态、可点「设置 Node 路径」）。

## 7. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| **S1** | 底座：动态实例/注册闸门、kind、配置描述符、apply 结果、detail_view 契约；`GET /api/connectors` 纳入 mod（additive） | API 同时列出内置连接器与 mod；未注册 id 不会误判启用 |
| **S2** | `ConfigEditor`（原型 1:1）+ 接到内置连接器与 mod | token-weather 默认值可见、可重置、可保存并看到重载结果；无配置 mod 不显示空 JSON |
| **S3** | 统一连接器页（内置 + **Claude Mods** 分组、共享详情抽屉、差异化 tab），移除 Mods 设置页签 | Chrome 有安装向导/连接状态，钉钉有会话策略，Claude Mod 有权限/活动 |
| **S4** | 权限与信任 | JS grants 可编辑；首次运行逐项确认；classic 危险命令可审 |
| **S5** | 健康与诊断 | Validate 可用；活动日志/unserved/重启可见 |
| **S6** | 会话内呈现面 | band Input/Select 值可达；ask 排队不覆盖；generation 仅 debug 显示 |

## 8. 风险与待定

1. **动态实例 × read_config 兜底**：必须加注册闸门，否则删除后的 mod 仍“启用”。
2. **PATCH 严格未知键**：mod 的动态键需要合成 schema 覆盖；否则给 mod kind 放宽。
3. **重载 ack 需要 Rust 改动**：没有 ack 的「已应用」是假的。
4. **secret 存储**：明文在 settings.json。是否引入钥匙串/secret store，需单独决策。
5. **项目级 mod**：`<workspace>/.ginno/mods` 与连接器单一配置块不匹配；L1+L2 先保持现状（项目副本优先），详情页标注 source。
6. **`$.store` standalone broker 缺 store path**：独立 broker 下不可用，需在 host 侧传路径。
7. **移除 Mods 页签**是破坏性 UI 变更：保留旧路由重定向到连接器页。

## 9. 顺带发现的确定性 bug（与设计独立，可先修）

- `ModsSettings` 成功消息恒红（`:448`）。
- `validate` 按钮必坏（socket 未实现）。
- band 的 Input/Select 值被丢弃（`ModBand.tsx:67`）。
- 同一会话的 mod ask 互相覆盖（`handle.ts:1186`）。
- classic grant 写回时展开旧对象，可能覆盖同 mod 的其他 grant 键（`ModsSettings.tsx:172-185`）。