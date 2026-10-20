# Mods Marketplace / Mod 商店产品设计

> 2026-10-21。状态:**产品方案(待评审)**。前置:`claude-code-mods-design.md`(兼容层)、
> 其缺口清单(2026-10-20 盘点:tool.* op 族、Input/Select/Markdown/Code 元素等)。
> 定位:分发与信任层,不解决 API 兼容度——兼容度用**评分显性化**,不阻塞上架。

## 0. TL;DR

- **形态:git 仓库即市场,零基建**。市场 = 一个 git repo,根目录 `.claude-plugin/marketplace.json`
  列出插件清单——**直接兼容 Claude Code 官方 marketplace 格式**,`claude-plugins-community`
  等社区仓库开箱即挂;Ginno 官方源起步用 `robscc/ginno-mods`。
- **入口:设置 → 连接 → Mods 页签**,顶部「浏览 Mods」进商店(抽屉/子视图),不新增顶层导航
  (2026-10-21 用户定稿)。
- **安装 ≠ 信任**:安装前强制过 broker validate + `$` 调用兼容度报告 + classic hooks 命令
  预览(danger 类默认拒),安装后 grants 逐条确认。
- 分三期:M1 只读浏览+本地安装打通;M2 在线安装+更新;M3 投稿/评分。

## 1. 目标与非目标

### 目标

1. 用户在产品内浏览、搜索、安装、更新、卸载 mod,全程不碰命令行。
2. 复用官方 Claude Code 插件生态的分发格式,社区 mod「格式开箱即挂,能力按兼容度评分明示」。
3. 安全边界不后退:JS mod 走 grants 管控;classic hooks(任意 shell)在安装时可见、运行时受白名单管。

### 非目标

- 不做中心化注册服务/账号/鉴权(v1 纯 git 只读拉取)。
- 不做 mod 内付费/签名链(git commit pin + 仓库星标/官方精选代替 trust 层)。
- 不做运行时自动更新(更新是显式动作;避免 mod 供应链静默变更)。
- 不解决「官方 mod 用了 Ginno 没实现的 API」——那是兼容层缺口清单的活,商店只负责**如实打分**。

## 2. 现状盘点(设计起点)

| 环节 | 现状 | 缺口 |
|---|---|---|
| 本地安装 | `POST /api/mods/install {path}`:校验 manifest → copytree 到 `~/.ginno/mods/<name>` → enabled | 只收**本地目录**,无远端拉取 |
| 发现 | `scan_installed_mods()` 扫 mods 目录;`GET /api/mods` 列表 | 无来源/版本元数据 |
| validate | `POST /api/mods/validate` → broker validate(hooks×events、$×实现) | 有,但不在安装路径上强制 |
| 兼容度报告 | ModsSettings `ModCompatView`(注册事件×触发、$ 调用×实现命中率) | 有;需前置到**安装前**(装后才能跑 → 需「试装」语义,见 §5.3) |
| classic 安全 | `classic_grant_for`/`classify_hook_command`(Python grants) | 安装前无命令预览 |
| 设置存储 | `mods.items.<name> {enabled, source: "global", grants, config}` | 无 version/origin/marketplace 字段 |

## 3. 市场格式(分发契约)

### 3.1 市场 = git 仓库

```jsonc
// <repo>/.claude-plugin/marketplace.json —— 兼容 Claude Code 官方 schema 的子集
{
  "name": "ginno-mods",
  "owner": { "name": "robscc" },
  "plugins": [
    {
      "name": "next-steps",
      "source": "./next-steps",          // 仓库内相对路径
      "description": "…",
      "version": "0.1.0",                // 可缺省,回落插件自己的 plugin.json
      "tags": ["productivity"]
    }
  ]
}
```

- **多源**:设置里可挂多个市场(有序);默认内置 `robscc/ginno-mods`(Ginno 精选,过兼容度
  验收)与可选的 `anthropics/claude-plugins-community`(全量,兼容度评分区分)。
- **source 扩展**:官方 schema 兼容为主;Ginno 允许 `source` 为 git URL(跨仓库引用,v2)。

### 3.2 单个 mod 的落盘形态不变

`.claude-plugin/plugin.json` + `hooks/hooks.json`(JS 形态)或 classic `hooks.json`
——与现有 `scan_installed_mods`/broker/classic dispatcher 完全一致。市场只是**目录的发现层**。

## 4. 产品流程

### 4.1 浏览(M1)

- 设置 → 连接 → **Mods 页签**:已装列表(现状)之上加「浏览 Mods」按钮 → 右侧抽屉(全高,
  与设置页同级滚动)。
- 抽屉:市场源切换(下拉)· 搜索框(name/description/tags 子串)· 卡片列表
  (name、description、★来源徽标「精选/社区」、兼容度评分徽章、[已装] 状态)。
- 卡片 → 详情:README(仓库内 `README.md`,渲染为 Markdown)、版本、兼容度明细
  (注册事件清单、`$` 调用清单逐条绿/红——红 = Ginno 未实现的 op,引用缺口清单文案)、
  classic hooks 命令预览表(命令 → danger 分类)。
- **兼容度评分**(三档,规则见 §5.3):`✅ 完整兼容` / `🟡 部分兼容(N 项降级)` / `🔴 不可用`。
  🔴 的安装按钮置灰但可强制(黄字警告)。

### 4.2 安装(M2)

1. git 浅克隆市场仓库到缓存(`~/.ginno/cache/mods-market/<source-hash>`,TTL 刷新);
2. 按 `source` 路径取插件目录 → **先跑 validate + 试装评分**(§5.3)→ 展示确认页:
   该 mod 会收到哪些事件、调用哪些 `$` 能力、grants 需确认项、classic 命令及 danger 分类;
3. 确认 → copytree 进 `~/.ginno/mods/<name>`(复用现有 install 语义)+ 写回来源元数据
   (见 §6 settings 扩展)+ classic hooks 重注册(现有逻辑);
4. 装后 JS mod 的 grants 首次确认走既有 Mods 页签流程。

### 4.3 更新(M2)

- 已装卡片显示「有更新:0.1.0 → 0.2.0」(市场清单 version / 插件 plugin.json 对比);
- 更新 = 重新拉取 → validate → **diff 展示**:版本变化、hooks.json 变更、classic 命令变更
  (新增 danger 命令必须再确认)、grants 变更 → 覆盖安装(config/grants 用户数据保留);
- 不做自动更新;市场清单刷新 = 打开抽屉时后台 `git fetch`(5 分钟 TTL)。

### 4.4 卸载(M1)

现有删除能力即可(Mods 页签已有);来源为市场的 mod 卸载时提示「重装可从商店一键回来」。

## 5. 信任与安全

### 5.1 安装前

- validate 必过(结构错误 = 🔴 不可安装);
- classic hooks 命令逐条列出,`classify_hook_command` 的 **danger 类(包管理器安装、settings
  写入、curl|sh)默认拒**——安装确认页显式列出,运行时白名单继续兜底(现有 classic grants);
- README 是 untrusted 文本:按站内 Markdown 渲染(无脚本执行,链接外跳确认)。

### 5.2 安装后

- JS mod:grants 逐 op 确认(现状,不放宽);
- 来源与版本 pin 记录在 settings(§6),更新 diff 是唯一变更通道;
- 卸载 = 删目录 + 清 settings item(现状)。

### 5.3 兼容度评分规则(前置的「试装」)

问题:validate/兼容度报告需要 mod 已加载,而安装前没有 runner。方案——**沙箱试装**:

1. 把市场缓存里的插件目录以 `__market_preview__` 临时名挂给 broker validate
   (validate 本就是无副作用加载:hello 握手 + 注册表收集,不执行 hooks);
2. 从 hello 的注册事件清单 + validate 的 $ 调用清单,对照 runtime op 表
   (`ops.py` 实装集)与 broker 本地 op 表算分:
   - 注册的**事件**全部在 events.py 实装集 → 事件维度 ✅;
   - `$` 调用逐条:broker 本地 / runtime 实装 → 绿;UNSERVED 或 no-implementation → 红;
   - 全绿 = ✅;有红但核心链路(该 mod 注册事件的处理器里没调红的 op)= 🟡;反之 🔴;
3. 试装 runner 用后即弃(每市场源一次,缓存结果进市场缓存的 meta json,按 commit hash 失效)。

## 6. 数据与 API

### 6.1 settings.json `mods` 扩展(向后兼容)

```jsonc
"mods": {
  "enabled": true,
  "marketplaces": [                       // 新增:市场源,有序
    { "name": "ginno-mods", "repo": "https://github.com/robscc/ginno-mods", "enabled": true },
    { "name": "claude-plugins-community", "repo": "…/claude-plugins-community", "enabled": false }
  ],
  "items": {
    "next-steps": {
      "enabled": true, "source": "global",
      "origin": {                          // 新增:市场来源元数据(本地安装无此键)
        "marketplace": "ginno-mods", "path": "./next-steps",
        "commit": "abc1234", "version": "0.1.0", "installed_at": 1761000000
      }
    }
  }
}
```

### 6.2 runtime API(挂在现有 mods router)

| 端点 | 语义 |
|---|---|
| `GET /api/mods/marketplace` | 列市场源 + 各自插件清单(缓存优先,`?refresh=1` 强拉) |
| `GET /api/mods/marketplace/item?src=&path=` | 单插件详情:manifest + README + 命令预览 |
| `POST /api/mods/marketplace/preview` | 试装评分(validate + 兼容度),返回评分与明细 |
| `POST /api/mods/marketplace/install` | {src, path, overwrite} → 现有 install 流 + origin 元数据 |
| `PUT/DELETE /api/mods/marketplace/sources` | 市场源增删(设置页管理) |

git 拉取:Python 侧直接 `git clone/fetch --depth 1`(桌面版已依赖 git 工具链的先例是
delegation 后端探测;无 git → 该市场源显示「不可用」并给原因)。

### 6.3 前端(IA,2026-10-21 定稿)

- 不新增设置页签/顶层导航:**Mods 页签**头部加「浏览 Mods」→ 抽屉(StoreDrawer);
- 抽屉内部三态:列表 / 详情 / 确认页;安装/更新完成后回到已装列表并高亮新条目;
- i18n:`settings.mods.store.*` 键族,en/zh-CN 同批。

## 7. 分期

| 期 | 内容 | 验收 |
|---|---|---|
| **M1**(~3 天) | 市场源内置 + 清单拉取缓存;抽屉浏览/搜索/详情;手动下载目录走现有本地安装;卸载提示 | 能从 `robscc/ginno-mods` 浏览到 mod 清单与 README |
| **M2**(~1 周) | 在线安装(origin 元数据)+ 试装评分 + classic danger 预览 + 更新 diff 流程 | next-steps 从商店点装即用;token-weather 显示「有更新」并完成一次 diff 更新 |
| **M3** | 投稿流程(PR 到 ginno-mods 的模板与 CI 校验)、评分/下载量(需要轻后端或 GitHub 元数据)、跨仓库 source | — |

## 8. 开放问题

1. 官方精选源的治理:ginno-mods 收录标准(兼容度 ✅ 为硬门槛?)——M2 前定。
2. `claude-plugins-community` 挂默认源与否:全量清单大且大量 🔴(依赖 Ginno 未实装 op),
   先默认不挂、抽屉里给「添加社区源」引导,M2 看评分数据再定。
3. 代理/离线环境:git 拉取失败的可解释降级文案;是否支持手动导入 .zip(M2 顺手)。
4. 评分缓存失效粒度:按 commit hash 足够,还是叠加 runtime 版本(兼容层升级后评分会变)——
   建议 key = (commit, runtime_version),升级后首开重算。

## 9. 决议记录

- 2026-10-21:入口定稿——设置 → 连接 → Mods 页签内,不新增顶层导航(用户)。
- 2026-10-21:分发形态定稿——git 仓库即市场、兼容官方 marketplace.json 子集(方案评审默认,
  反对意见见开放问题 2)。
