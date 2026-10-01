# Connector Module Design(连接器模块)

> 状态:**已实施**(2026-10-02,M1+M2+M3 见文末)。目标:把「Ginno 与外部世界的能力连接」
> 独立成一个一等模块——连接状态、安装指引、简单配置集中在一处;Chrome 浏览器
> 扩展是第一个连接器,后续连接器(设备、外部服务、第三方 harness)复用同一框架。
>
> 关联文档:
> - `browser-companion-extension-design.md` — 首个连接器(Chrome 扩展)的完整
>   技术设计;其 §7.3 安装引导与连接状态**迁入本模块承载**,本文只定义宿主框架。
> - `external-agents-design.md` — 外部 agent 委派是工具不是连接器(边界见 §1)。

---

## 0. TL;DR

- **定位**:Connector = Ginno 与一个外部能力源的双向连接单元(自己的安装引导、
  连接状态机、版本、capabilities、配置项)。模块管「连没连、怎么装、怎么配」,
  不管连接器内部怎么干活(那是各连接器自己的设计文档)。
- **UI**:左下角侧栏 footer nav,**Knowledge Base 上方**新增 `Connectors` 入口
  (带聚合状态点),路由 `/connectors`(对齐 `/kb` 的 workspace 外路由模式);
  每个连接器一张卡片:状态徽标 + 版本 + 操作(安装引导/配置/启停)。
- **架构**:sidecar 持有 `ConnectorRegistry`(状态权威,连接器各自上报),前端
  订阅状态事件 + 调配置 API;配置进 `settings.json` 的 `connectors` 段。
- **首个连接器**:Chrome 扩展(把 browser 文档 §7.3 的 5 步向导、状态常驻、
  排查文案表挂进来;「B 轨降级开关」落为该连接器的配置项——顺带裁掉
  browser 文档 §8.1 的残留子项)。

## 1. 模块定位与边界

**Connector 是什么**:一个外部能力源与 Ginno 的连接。判据(全部满足才算):
- 有**生命周期**(要安装/启用/连接/断连,而不是随叫随到);
- 有**持久状态**(连接状态、版本、能力集,值得让用户看见);
- 有**面向用户的配置**(启停、开关、名单)。

**不是 Connector 的**(防止概念膨胀):
- `web_search` / `web_fetch` — 无状态 API 调用,留在 web_tools;
- MCP server / external agent 委派 — 工具层概念,挂在 mcp/ 与 external_agent.py;
- Context Folders — 已有自己的 Chip 交互(ContextFoldersChip),不收编。

候选后续连接器(仅列举,不承诺):本地设备(iPhone/手机接力)、IDE 集成、
IM 通知渠道、dsh 类 harness 接入(与「占协议端点」策略衔接)。

## 2. UI 设计

### 2.1 入口:左下角,Knowledge Base 上方

```
┌─ sidebar footer(nav-item 列表)────────────┐
│ ●  Connectors        ← 新增,● = 聚合状态点  │
│ 📖 Knowledge Base                          │
│ ⚙  Workflows                               │
│ ⚙  Settings                                │
└────────────────────────────────────────────┘
```

- 位置:footer nav **第一项(KB 上面)**,`AppShell.tsx` footer 区加一条
  `<Link href="/connectors">`,图标用 Plug/Link2 类;
- **聚合状态点**:全部连接器正常(或全部未启用)→ 不显示;有「已启用但未连接」
  → 黄点;有错误 → 红点。一眼判断「我的浏览器连着没」;
- 路由 `/connectors`,走 `AppShell` 的非 workspace 分支(与 `/kb` 同构:
  workspace 保持挂载、WS/草稿状态存活)。

### 2.2 连接器卡片(`/connectors` 页)

每连接器一张卡片,纵向排列:

```
┌──────────────────────────────────────────────┐
│ 🌐 Chrome 浏览器扩展                    ✅已连接│
│ v1.0.2 · Chrome · 扩展 ID ginn…k3q           │
│ 用你自己的 Chrome 替你干活:登录态共享、可接管。  │
│                                              │
│ [安装指引] [配置 ▾] [⋯ 禁用]                  │
└──────────────────────────────────────────────┘
```

- 状态徽标(§4 状态机):`未安装`(灰)/`安装中`(蓝转圈)/`已连接`(绿)/
  `已断开`(黄)/`错误`(红)/`已禁用`(灰);
- 版本行:扩展版本 + 浏览器类型 + 短 ID(排障对齐用,来自 `extensionInfo`);
- 主操作随状态变化:`未安装` → [开始安装](向导);`已断开` → [排查];
  `已连接` → [配置];
- 卡片本体不堆配置——**配置点开折叠区或弹层**,保持列表页干净。

### 2.3 安装指引(向导,弹层)

Browser 文档 §7.3 的 5 步向导(Step 0 说明 → 扩展就位/Finder →
chrome://extensions → 开发者模式 → 加载 → 自动连接确认)以**全屏弹层**承载,
进度存 connector 状态,可中断重入。排查文案表(§7.3)作为 Step 5 超时后的
展开区。向导由连接器声明自己的 steps(§3 接口),模块只负责壳(步骤条、
上一步/下一步、连接等待动效)。

## 3. Connector 抽象(sidecar)

```python
# packages/runtime/src/ginno_runtime/connectors/
class Connector(Protocol):
    id: str                  # "chrome-extension"
    name: str                # "Chrome 浏览器扩展"
    version: str | None      # 对端上报的版本(None = 未安装)
    status: ConnectorStatus  # 状态机 §4,registry 为权威
    capabilities: list[str]  # 对端上报(如 relay 的 capabilities 数组)

    def config_schema(self) -> dict: ...          # 简单配置的 schema
    def config(self) -> dict: ...                 # 读 settings.json connectors 段
    async def set_config(self, cfg: dict) -> None: ...
    def install_steps(self) -> list[InstallStep]: ...  # 前端向导渲染用
```

- `ConnectorRegistry`(sidecar 单例):注册、状态聚合(给侧栏状态点)、
  事件广播(状态变更推前端);
- 浏览器连接器 = `browser/` 模块的 relay 端点实现该接口;状态判定沿用
  browser 文档 §7.3 的顺序:**WS 已连 > native messaging 已连 > 都没有**,
  避免把网络抖动误报成安装问题;
- 配置存储:`settings.json` 新增 `connectors: {"chrome-extension": {...}}`,
  前端经 sidecar API 读写(不直接碰文件),schema 校验在 set_config。

### Chrome 扩展连接器的初始配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关,关掉=工具面不注入、状态点消失 |
| `fallback_profile_mode` | `"ask"` | 未装扩展时 B 轨(专用 profile Chrome)的启用方式:`ask`(提示一次)/`auto`(静默降级)/`off`。**即 browser 文档 §8.1 残留项的落点** |
| `sensitive_domains` | 内置名单 | 高危域(支付/邮箱/云控制台)动作类工具强制 ask 的追加名单 |
| `auto_open_group` | `true` | agent 开新标签时是否把 Chrome 窗口带前置焦 |

## 4. 状态机

```
未安装 ──开始安装──▶ 安装中 ──检测到连接──▶ 已连接
  ▲                   │超时/失败              │  ╲
  │                   ▼                      ▼   ╲
  └──────────────── 未连接 ◀──(对端消失/Chrome 重启)─ ╲
                        │ 重连成功 → 已连接            ╲
任何状态 ──用户禁用──▶ 已禁用 ──启用──▶ 回到之前状态
任何状态 ──不可恢复错误──▶ 错误(带文案 + 动作,§7.3 排查表)
```

- 转移由连接器上报,registry 去抖(断连 3s 内重连不闪黄);
- 状态持久化不落盘:重启后按实际探测重建(连接器的连接本身就是事实源);
  唯一持久化的是 `enabled` 与配置。

## 5. 前后端接口

- `GET /api/connectors` → 列表(id/name/status/version/capabilities/config);
- `PATCH /api/connectors/{id}/config` → set_config;
- 状态变更:复用现有事件通道(sidecar → web 的推送流)加
  `connector_status_changed` 事件,前端 store 更新侧栏状态点与卡片;
  兜底 15s 轮询(事件通道未建好前的 M1 实现)。

## 6. 里程碑(并入 browser 双轨)

- **M1**:模块骨架(Registry + `/connectors` 路由 + 侧栏入口 + 状态点)+
  Chrome 扩展卡片(状态/版本/禁用,安装向导为「即将推出」占位)——随 B 轨
  一起交付,B 轨模式同样以连接器卡片呈现(`fallback_profile_mode=auto` 时
  Chrome 实例卡显示「已连接(内置实例)」)。
- **M2**:5 步安装向导全量(弹层壳 + steps 渲染)+ 排查文案表 + 配置折叠区;
  随扩展轨上线。
- **M3**:native messaging 端口发现接入(状态判定升级);第二个连接器试点
  (从 §1 候选里挑,验证抽象是否需要返工)。

## 7. 待裁定

1. 卡片内的配置交互:折叠区 vs 弹层(倾向折叠区,配置项都很少);
2. `fallback_profile_mode` 的默认值(`ask` 提示一次 vs `auto` 静默)——
   涉及「Ginno 未经说明拉起 Chrome 进程」的感知边界,建议默认 `ask`;
3. 侧栏入口命名:`Connectors` vs `连接器` vs `集成`(现产品侧栏是英文,倾向
   Connectors)。


---

## 9. 实现说明(2026-10-02)

| 设计项 | 实现位置 |
|---|---|
| Registry + 状态机 + 去抖 + 聚合点 | `packages/runtime/src/ginno_runtime/connectors/registry.py`(idle 语义:B 轨未运行不闪黄) |
| 内置连接器 + 安装步骤文案 | `connectors/builtin.py`(chrome-extension 含 fallback_profile_mode/sensitive_domains/confirmed_domains) |
| API | `api/connectors.py`(GET 列表/详情、PATCH config、POST action:reveal_folder/handoff_release/confirm_domain/profile_start/stop) |
| 扩展轨 relay 宿主 | 同文件 `@router.websocket("/extension"〔/v2〕)` → `browser/relay.py` |
| 侧栏入口(KB 上方 + 聚合状态点) | `apps/web/src/components/shell/AppShell.tsx`(footer nav 首项,10s 轮询) |
| `/connectors` 页(卡片/配置折叠/向导弹层/排查) | `apps/web/src/app/connectors/page.tsx` + `components/connectors/{InstallWizard,ConfigFold}.tsx` |
| 接管横幅(「已接管,继续」) | Connectors 页顶部,poll `GET /api/connectors/browser/handoff` |

说明:M1 的状态推送用了轮询兜底(页面 5s/侧栏 10s/向导等待 1s),事件通道
(`connector_status_changed`)的 WS 推送留作后续优化——设计 §5 的形状已定。
